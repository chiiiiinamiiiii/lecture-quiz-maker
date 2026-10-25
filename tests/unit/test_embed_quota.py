"""関係の補完を速くする（Refs #107）。HTTP は差し替え、API は呼ばない。

倫理学 17 回の実測（519 秒）で、関係の補完に 251 秒かかった。内訳は、検索の埋め込み 139 件で 429 が 3 回
（待ち約 135 秒）と、Wikipedia の検索 約 50 秒。確かめること:
- 429 の記録に、どの枠か（quotaId・quotaMetric）が出る
- 429 のたびに 1 分あたりの上限を下げて覚え、その後はその上限の内側で送る（件数の枠なら件数、トークンの枠ならトークン数）
- 問い合わせ文はまとめて送る（1 回に最大 EMBED_QUERY_BATCH 件。件数の上限も超えない）。同じ文は 1 度だけ、
  2 度目の取り込みではキャッシュから読んで送らない
- Wikipedia の検索は同時に WIKI_WORKERS 本まで並べる。結果は lookup と同じで、キャッシュは最後に 1 度書く
時計は conftest の fake_embed_clock（本当には眠らない）。
"""
from __future__ import annotations

import threading
import time

import numpy as np
import pytest
import requests

from quizmap import common, rag
from quizmap.common import LLMClient


class Resp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload
        self.text = str(payload)

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


def quota_429(quota_id: str, metric: str, retry: float = 20.0) -> Resp:
    return Resp(429, {"error": {"message": f"Quota exceeded. Please retry in {retry}s.", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": quota_id, "quotaMetric": metric}]}]}})


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(common, "MIN_CALL_INTERVAL", 0.0)
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    return LLMClient(api_key="k", base_url="https://example.invalid/v1beta")


@pytest.fixture
def api(monkeypatch, fake_embed_clock):
    """batchEmbedContents の偽物。limit を決めると、直近 60 秒（偽の時計）に通した件数が limit を超える送信を 429 にする。"""
    state = {"sent": [], "ok": [], "limit": None, "quota": ("EmbedContentRequestsPerMinutePerProjectPerModel-FreeTier",
                                                              "generativelanguage.googleapis.com/embed_content_requests")}

    def post(url, headers=None, json=None, timeout=None):
        t = fake_embed_clock["t"]
        texts = [r["content"]["parts"][0]["text"] for r in json["requests"]]
        state["sent"].append({"t": t, "n": len(texts)})
        if state["limit"] is not None:
            used = sum(n for (s, n) in state["ok"] if t - s < 60.0)
            if used + len(texts) > state["limit"]:
                return quota_429(*state["quota"])
        state["ok"].append((t, len(texts)))
        return Resp(200, {"embeddings": [{"values": [float(len(x)), 1.0]} for x in texts]})

    monkeypatch.setattr(requests, "post", post)
    return state


# ------------------------------------------------------------------ 枠の名前と上限の下げ方

def test_quota_names_from_error_message():
    msg = "Quota exceeded. [quota: EmbedContentRequestsPerMinute-FreeTier, x/embed_content_requests]"
    assert rag.quota_names(msg) == "EmbedContentRequestsPerMinute-FreeTier, x/embed_content_requests"
    assert rag.quota_names("Quota exceeded.") == ""
    assert rag.quota_names(None) == ""


def test_429_logs_quota_name_and_lowers_item_limit(client, api, capsys):
    api["limit"] = 40
    vecs = rag.embed(client, [f"問い{i}" for i in range(60)], task="RETRIEVAL_DOCUMENT")
    assert len(vecs) == 60 and all(v for v in vecs)
    err = capsys.readouterr().err
    assert "枠: EmbedContentRequestsPerMinutePerProjectPerModel-FreeTier" in err
    assert "件数の上限 80 →" in err
    assert rag._limits["items"] < 80
    assert rag._limits["tokens"] == rag.EMBED_TOKENS_PER_MIN       # 件数の枠なのでトークン数は下げない


def test_token_quota_lowers_token_limit(client, api, capsys):
    api["limit"] = 40
    api["quota"] = ("EmbedContentInputTokensPerModelPerMinute-FreeTier", "x/embed_content_input_tokens")
    rag.embed(client, [f"問い{i}" for i in range(60)])
    assert "トークン数の上限" in capsys.readouterr().err
    assert rag._limits["tokens"] < rag.EMBED_TOKENS_PER_MIN
    assert rag._limits["items"] == rag.EMBED_ITEMS_PER_MIN


def test_unknown_quota_lowers_item_limit(client, monkeypatch, fake_embed_clock, capsys):
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append(len(json["requests"]))
        if len(calls) == 1:
            return Resp(429, {"error": {"message": "Resource exhausted"}})
        return Resp(200, {"embeddings": [{"values": [1.0, 0.0]} for _ in json["requests"]]})

    monkeypatch.setattr(requests, "post", post)
    rag.embed(client, [f"文{i}" for i in range(20)])
    assert "枠: 不明" in capsys.readouterr().err
    assert rag._limits["items"] == int(20 * rag.EMBED_LIMIT_SHRINK)   # 直近 1 分に送った 20 件の 3/4


def test_limit_is_never_raised_and_has_a_floor():
    rag._sent.clear()
    rag._sent.append((rag._now(), 1, 1))
    for _ in range(10):
        rag._lower_limit_after_429("x")
    assert rag._limits["items"] == rag.EMBED_MIN_ITEMS_PER_MIN
    rag._sent.append((rag._now(), 500, 1))                          # 量が多くても、上げはしない
    rag._lower_limit_after_429("x")
    assert rag._limits["items"] == rag.EMBED_MIN_ITEMS_PER_MIN


def test_after_lowering_later_sends_stay_inside(client, api):
    """件数の枠が実は 40 件のとき: 最初に 1 度 429 を受けた後は、上限の内側で送って 429 が続かない。"""
    api["limit"] = 40
    rag.embed(client, [f"文{i}" for i in range(300)])
    n429 = len(api["sent"]) - len(api["ok"])
    assert n429 <= 2
    later = [s for s in api["sent"] if s["t"] > api["sent"][0]["t"] + 120]
    assert later and all(s["n"] <= rag._limits["items"] for s in later)
    assert sum(n for _, n in api["ok"]) == 300


# ------------------------------------------------------------------ 問い合わせ文のまとめ方

def _index():
    pages = [{"lecture": 1, "page": n, "text": f"ページ{n}"} for n in range(1, 6)]
    return rag.SlideIndex(pages, np.array([[1.0, float(n)] for n in range(5)], dtype=np.float32))


def test_139_queries_go_in_two_requests(client, api):
    """補完の 139 件は、件数の上限 80 件を超えない 2 回で送る（前は 20 件ずつ 7 回）。"""
    res = _index().search_many(client, [f"概念{i}" for i in range(139)], k=2)
    assert [s["n"] for s in api["sent"]] == [80, 59]
    assert len(res) == 139 and all(len(r) == 2 for r in res)


def test_duplicate_queries_sent_once_and_cached_across_ingests(client, api):
    queries = [f"概念{i % 30}" for i in range(90)]
    _index().search_many(client, queries, k=1)
    assert sum(s["n"] for s in api["sent"]) == 30
    api["sent"].clear()
    _index().search_many(client, queries, k=1)                     # 別の取り込み（新しい索引）でも
    assert api["sent"] == []


def test_query_batch_follows_lowered_limit(client, api):
    rag._limits["items"] = 30
    _index().search_many(client, [f"概念{i}" for i in range(70)], k=1)
    assert [s["n"] for s in api["sent"]] == [30, 30, 10]


# ------------------------------------------------------------------ Wikipedia の並列

@pytest.fixture
def wiki_api(monkeypatch):
    """MediaWiki API の偽物。同時に何本走っているかを数える。語「無し」は見つからない。"""
    state = {"active": 0, "max": 0, "gets": 0, "agents": set()}
    lock = threading.Lock()

    def get(url, params=None, headers=None, timeout=None):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
            state["gets"] += 1
            state["agents"].add((headers or {}).get("User-Agent"))
        time.sleep(0.02)
        with lock:
            state["active"] -= 1
        if params.get("list") == "search":
            term = params["srsearch"]
            return Resp(200, {"query": {"search": [] if term == "無し" else [{"title": term}]}})
        title = params["titles"]
        return Resp(200, {"query": {"pages": [{"title": title, "extract": f"{title} の説明",
                                               "fullurl": f"https://ja.wikipedia.org/wiki/{title}"}]}})

    monkeypatch.setattr(requests, "get", get)
    return state


def test_lookup_many_runs_in_parallel_with_same_results(tmp_path, wiki_api, monkeypatch):
    terms = [f"語{i}" for i in range(12)] + ["無し", "語0"]
    wiki = rag.Wiki(tmp_path, "ja")
    saves = []
    monkeypatch.setattr(wiki, "_save", lambda: saves.append(1))
    got = wiki.lookup_many(terms)
    assert list(got) == list(dict.fromkeys(terms))
    assert got["無し"] is None and got["語3"]["title"] == "語3"
    assert 1 < wiki_api["max"] <= rag.WIKI_WORKERS
    assert wiki_api["agents"] == {rag.WIKI_USER_AGENT}
    assert len(saves) == 1                                           # キャッシュは最後に 1 度だけ書く
    assert wiki_api["gets"] == 12 * 2 + 1                           # 同じ語は 1 度だけ引く


def test_lookup_many_uses_cache_file(tmp_path, wiki_api):
    rag.Wiki(tmp_path, "ja").lookup_many(["語1", "語2"])
    before = wiki_api["gets"]
    again = rag.Wiki(tmp_path, "ja").lookup_many(["語1", "語2"])
    assert wiki_api["gets"] == before and again["語2"]["title"] == "語2"


def test_lookup_many_empty(tmp_path, wiki_api):
    assert rag.Wiki(tmp_path, "ja").lookup_many([]) == {}
    assert not (tmp_path / rag.WIKI_CACHE_FILE).exists()
