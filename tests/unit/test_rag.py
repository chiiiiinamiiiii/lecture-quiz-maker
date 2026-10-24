"""quizmap.rag: 埋め込み・スライドの索引・Wikipedia。HTTP は差し替え、API も Wikipedia も呼ばない。"""
from __future__ import annotations

import json
import zlib

import numpy as np
import pytest
import requests

from quizmap import common, rag
from quizmap.common import LLMClient

DIM = 32


def fake_vector(text: str) -> list[float]:
    """文字ごとに決まった次元を 1 増やす。同じ文字を多く含む文どうしが近くなる。"""
    v = [0.0] * DIM
    for ch in text:
        v[zlib.crc32(ch.encode()) % DIM] += 1.0
    return v


class FakeResponse:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture
def client():
    return LLMClient(api_key="test-key", base_url="https://example.invalid/v1beta")


@pytest.fixture
def fake_embed_api(monkeypatch):
    """batchEmbedContents の偽物。送られた本文を記録し、fake_vector を返す。"""
    calls: list[dict] = []
    monkeypatch.setattr(common, "MIN_CALL_INTERVAL", 0.0)

    def post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "body": json, "headers": headers})
        return FakeResponse(200, {"embeddings": [
            {"values": fake_vector(r["content"]["parts"][0]["text"])} for r in json["requests"]
        ]})

    monkeypatch.setattr(requests, "post", post)
    return calls


PAGES = [
    {"lecture": 1, "page": 1, "text": "功利主義 最大多数の最大幸福 ベンサム"},
    {"lecture": 1, "page": 2, "text": "カントの定言命法 義務 道徳法則"},
    {"lecture": 2, "page": 1, "text": "アリストテレスの徳 エートス 習慣"},
    {"lecture": 2, "page": 2, "text": ""},
]


# ---------------------------------------------------------------- embed

def test_embed_sends_batches_of_100_in_order(client, fake_embed_api):
    texts = [f"文{i}" for i in range(230)]
    vecs = rag.embed(client, texts, task="RETRIEVAL_QUERY")
    assert [len(c["body"]["requests"]) for c in fake_embed_api] == [100, 100, 30]
    assert vecs == [fake_vector(t) for t in texts]
    first = fake_embed_api[0]
    assert first["url"] == f"https://example.invalid/v1beta/models/{rag.EMBED_MODEL}:batchEmbedContents"
    req = first["body"]["requests"][0]
    assert req["model"] == f"models/{rag.EMBED_MODEL}"
    assert req["taskType"] == "RETRIEVAL_QUERY"
    assert first["headers"]["x-goog-api-key"] == "test-key"


def test_embed_skips_empty_text_and_returns_zero_vector(client, fake_embed_api):
    vecs = rag.embed(client, ["", "倫理", "  "])
    assert len(fake_embed_api) == 1 and len(fake_embed_api[0]["body"]["requests"]) == 1
    assert vecs[0] == [0.0] * DIM and vecs[2] == [0.0] * DIM
    assert vecs[1] == fake_vector("倫理")


def test_embed_retries_on_429(client, monkeypatch):
    monkeypatch.setattr(common, "MIN_CALL_INTERVAL", 0.0)
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    replies = [FakeResponse(429, {"error": {"message": "quota"}}),
               FakeResponse(200, {"embeddings": [{"values": [1.0, 0.0]}]})]
    monkeypatch.setattr(requests, "post", lambda *a, **k: replies.pop(0))
    assert rag.embed(client, ["x"]) == [[1.0, 0.0]]
    assert replies == []


def test_embed_rejects_wrong_count(client, monkeypatch):
    monkeypatch.setattr(common, "MIN_CALL_INTERVAL", 0.0)
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(200, {"embeddings": []}))
    with pytest.raises(RuntimeError):
        rag.embed(client, ["x"])


# ---------------------------------------------------------------- SlideIndex

def test_build_and_search_ranks_by_cosine(client, fake_embed_api):
    index = rag.SlideIndex.build(client, PAGES)
    assert len(fake_embed_api) == 1
    assert fake_embed_api[0]["body"]["requests"][0]["taskType"] == "RETRIEVAL_DOCUMENT"
    hits = index.search(client, "定言命法と義務", k=2)
    assert fake_embed_api[-1]["body"]["requests"][0]["taskType"] == "RETRIEVAL_QUERY"
    assert [(h["lecture"], h["page"]) for h in hits][0] == (1, 2)
    assert len(hits) == 2
    assert set(hits[0]) == {"lecture", "page", "text", "score"}
    assert hits[0]["score"] >= hits[1]["score"]
    assert -1.0 <= hits[1]["score"] <= hits[0]["score"] <= 1.0 + 1e-3


def test_search_exclude_and_empty_pages(client, fake_embed_api):
    index = rag.SlideIndex.build(client, PAGES)
    hits = index.search(client, "定言命法と義務", k=10, exclude={(1, 2)})
    keys = [(h["lecture"], h["page"]) for h in hits]
    assert (1, 2) not in keys
    assert (2, 2) not in keys                    # 本文が空のページは返さない
    assert len(keys) == 2


def test_search_reuses_query_embedding(client, fake_embed_api):
    index = rag.SlideIndex.build(client, PAGES)
    index.search(client, "徳", k=1)
    index.search(client, "徳", k=3)
    assert len(fake_embed_api) == 2              # 索引づくり 1 回 + 問い合わせ 1 回


def test_save_load_roundtrip_is_compact(client, fake_embed_api, tmp_path):
    index = rag.SlideIndex.build(client, PAGES)
    index.save(tmp_path)
    path = tmp_path / "rag_index.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["model"] == rag.EMBED_MODEL and data["dim"] == DIM
    assert isinstance(data["vectors"], str)       # float16 を base64 で 1 本の文字列に
    assert data["pages"][0] == PAGES[0]

    loaded = rag.SlideIndex.load(tmp_path)
    assert loaded is not None and len(loaded) == len(PAGES)
    assert loaded.pages == index.pages
    np.testing.assert_array_equal(loaded.vectors, index.vectors)
    a = index.search(client, "アリストテレス", k=3)
    b = loaded.search(client, "アリストテレス", k=3)
    assert [(h["lecture"], h["page"]) for h in a] == [(h["lecture"], h["page"]) for h in b]


def test_saved_size_for_185_pages_of_3072_dims(tmp_path):
    rng = np.random.default_rng(0)
    pages = [{"lecture": i // 10 + 1, "page": i % 10 + 1, "text": "あ" * 450} for i in range(185)]
    index = rag.SlideIndex(pages, rng.standard_normal((185, 3072)).astype(np.float32))
    index.save(tmp_path)
    size = (tmp_path / "rag_index.json").stat().st_size
    assert size < 3 * 1024 * 1024


def test_load_missing_broken_or_other_model(tmp_path, client, fake_embed_api, monkeypatch):
    assert rag.SlideIndex.load(tmp_path) is None
    (tmp_path / "rag_index.json").write_text("{broken", encoding="utf-8")
    assert rag.SlideIndex.load(tmp_path) is None
    rag.SlideIndex.build(client, PAGES).save(tmp_path)
    monkeypatch.setattr(rag, "EMBED_MODEL", "other-embedding")
    assert rag.SlideIndex.load(tmp_path) is None


def test_build_with_no_pages(client, fake_embed_api, tmp_path):
    index = rag.SlideIndex.build(client, [])
    assert len(index) == 0 and fake_embed_api == []
    assert index.search(client, "何か") == []
    index.save(tmp_path)
    assert len(rag.SlideIndex.load(tmp_path)) == 0


# ---------------------------------------------------------------- Wiki

@pytest.fixture
def fake_wiki(monkeypatch):
    """MediaWiki API の偽物。「無い語」は検索結果 0 件、「落ちる語」は通信エラー。"""
    calls: list[dict] = []

    def get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params, "headers": headers})
        if params.get("list") == "search":
            term = params["srsearch"]
            if term == "落ちる語":
                raise requests.ConnectionError("down")
            hits = [] if term == "無い語" else [{"title": f"{term}（記事）"}]
            return FakeResponse(200, {"query": {"search": hits}})
        title = params["titles"]
        return FakeResponse(200, {"query": {"pages": [{
            "title": title, "extract": f"{title}の冒頭。",
            "fullurl": f"https://ja.wikipedia.org/wiki/{title}",
        }]}})

    monkeypatch.setattr(requests, "get", get)
    return calls


def test_wiki_lookup_and_cache(tmp_path, fake_wiki):
    wiki = rag.Wiki(tmp_path, "ja")
    r = wiki.lookup("功利主義")
    assert r == {"title": "功利主義（記事）", "url": "https://ja.wikipedia.org/wiki/功利主義（記事）",
                 "extract": "功利主義（記事）の冒頭。"}
    assert len(fake_wiki) == 2
    assert fake_wiki[0]["url"] == "https://ja.wikipedia.org/w/api.php"
    assert "LectureQuizMaker" in fake_wiki[0]["headers"]["User-Agent"]
    assert fake_wiki[1]["params"]["exintro"] and fake_wiki[1]["params"]["explaintext"]

    assert wiki.lookup("功利主義") == r          # 二度目は問い合わせない
    assert len(fake_wiki) == 2
    again = rag.Wiki(tmp_path, "ja")             # ファイルのキャッシュも効く
    assert again.lookup("功利主義") == r
    assert len(fake_wiki) == 2


def test_wiki_not_found_is_cached_as_none(tmp_path, fake_wiki):
    wiki = rag.Wiki(tmp_path, "ja")
    assert wiki.lookup("無い語") is None
    assert wiki.lookup("無い語") is None
    assert len(fake_wiki) == 1
    assert json.loads((tmp_path / "wiki_cache.json").read_text(encoding="utf-8")) == {"ja": {"無い語": None}}


def test_wiki_network_error_returns_none_and_is_not_cached(tmp_path, fake_wiki):
    wiki = rag.Wiki(tmp_path, "ja")
    assert wiki.lookup("落ちる語") is None
    assert wiki.lookup("落ちる語") is None
    assert len(fake_wiki) == 2                   # 失敗は覚えず、次も試す
    assert not (tmp_path / "wiki_cache.json").exists()


def test_wiki_uses_language_and_keeps_other_language_cache(tmp_path, fake_wiki):
    rag.Wiki(tmp_path, "ja").lookup("倫理学")
    en = rag.Wiki(tmp_path, "en")
    en.lookup("Ethics")
    assert fake_wiki[-1]["url"] == "https://en.wikipedia.org/w/api.php"
    data = json.loads((tmp_path / "wiki_cache.json").read_text(encoding="utf-8"))
    assert set(data) == {"ja", "en"} and "倫理学" in data["ja"] and "Ethics" in data["en"]


def test_wiki_empty_term(tmp_path, fake_wiki):
    assert rag.Wiki(tmp_path, "ja").lookup("  ") is None
    assert fake_wiki == []
