"""rag.embed の量の調整（無料枠の 1 分あたりの上限、429）。HTTP は差し替え、API は呼ばない。

利用者が英語の資料 285 ページを取り込んだとき、100 ページを 1 回で送って 1 分あたり 3 万トークンの枠を
1 回で超え、429 が続いて索引づくりが止まった。確かめること:
- バッチは件数・文字数・トークン数（見積もり）のどれも上限を超えない
- 直前 60 秒に送ったトークン数・件数が、1 分あたりの上限を超えない（待つ）
- 429 ならバッチを半分に割って送り直す。本文の「retry in N s」の秒数だけ待つ
- 進み具合をバッチごとに知らせる
- 1 件でも 429 が続けば LLMError。取り込み全体は止まらない（test_ingest_rag 側）
時計は conftest の fake_embed_clock（本当には眠らない）。
"""
from __future__ import annotations

import pytest
import requests

from quizmap import common, rag
from quizmap.common import LLMClient, LLMError


class Resp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload
        self.text = str(payload)

    def json(self):
        return self._p


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(common, "MIN_CALL_INTERVAL", 0.0)
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    return LLMClient(api_key="k", base_url="https://example.invalid/v1beta")


@pytest.fixture
def api(monkeypatch, fake_embed_clock):
    """batchEmbedContents の偽物。reject(本文の列) が文字列を返せば 429（その文字列が本文）。"""
    state = {"sent": [], "reject": lambda texts: None}

    def post(url, headers=None, json=None, timeout=None):
        texts = [r["content"]["parts"][0]["text"] for r in json["requests"]]
        state["sent"].append({"t": fake_embed_clock["t"], "texts": texts})
        msg = state["reject"](texts)
        if msg:
            return Resp(429, {"error": {"message": msg}})
        return Resp(200, {"embeddings": [{"values": [float(len(t)), 1.0]} for t in texts]})

    monkeypatch.setattr(requests, "post", post)
    return state


def english_pages(n=285, chars=690):
    word = "attention transformer token embedding "
    return [(word * (chars // len(word) + 1))[:chars - 4] + f" {i:03d}" for i in range(n)]


# ------------------------------------------------------------------ 見積もりと分け方

def test_approx_tokens():
    assert rag.approx_tokens("倫理学の世界") == 6                 # 日本語は 1 字 1 トークン
    assert rag.approx_tokens("a" * 400) == 100                   # 英語は 4 字 1 トークン
    assert rag.approx_tokens("カント Kant") == 3 + 2


def test_split_batches_respects_all_limits_and_order():
    items = list(enumerate(["x" * 100] * 7))
    assert [len(b) for b in rag.split_batches(items, max_items=3, max_chars=10**6, max_tokens=10**6)] == [3, 3, 1]
    assert [len(b) for b in rag.split_batches(items, max_items=100, max_chars=250, max_tokens=10**6)] == [2, 2, 2, 1]
    ja = list(enumerate(["倫" * 100] * 5))                       # 100 トークンずつ
    assert [len(b) for b in rag.split_batches(ja, max_items=100, max_chars=10**6, max_tokens=250)] == [2, 2, 1]
    flat = [i for b in rag.split_batches(items, max_items=3) for i, _ in b]
    assert flat == list(range(7))


def test_split_batches_oversize_item_goes_alone():
    items = [(0, "a"), (1, "b" * 500), (2, "c")]
    assert [[i for i, _ in b] for b in rag.split_batches(items, max_items=10, max_chars=100, max_tokens=10**6)] \
        == [[0], [1], [2]]


def test_default_batch_fits_free_tier():
    """285 ページの英語（1 ページ約 690 字）を既定の上限で分けると、どのバッチも 2 万トークン・2 万字・20 件以内。"""
    batches = rag.split_batches(list(enumerate(english_pages())))
    for b in batches:
        assert len(b) <= rag.EMBED_BATCH
        assert sum(len(t) for _, t in b) <= rag.EMBED_BATCH_CHARS
        assert sum(rag.approx_tokens(t) for _, t in b) <= rag.EMBED_BATCH_TOKENS
    assert rag.EMBED_BATCH_TOKENS <= 20000 and rag.EMBED_TOKENS_PER_MIN < 30000


# ------------------------------------------------------------------ 1 分あたりの量

def _window_max(sent, key):
    worst = 0
    for s in sent:
        worst = max(worst, sum(key(x) for x in sent if s["t"] <= x["t"] < s["t"] + 60.0))
    return worst


def test_285_english_pages_stay_under_per_minute_limits(client, api, fake_embed_clock):
    pages = english_pages()
    vecs = rag.embed(client, pages)
    assert len(vecs) == 285 and vecs[0] == [float(len(pages[0])), 1.0]
    tokens = _window_max(api["sent"], lambda x: sum(rag.approx_tokens(t) for t in x["texts"]))
    items = _window_max(api["sent"], lambda x: len(x["texts"]))
    assert tokens <= rag.EMBED_TOKENS_PER_MIN
    assert items <= rag.EMBED_ITEMS_PER_MIN
    assert fake_embed_clock["sleeps"], "1 分の枠を超えないよう待ったはず"
    assert sum(len(x["texts"]) for x in api["sent"]) == 285       # 429 は出ていない（送り直し 0）


def test_pace_is_shared_across_calls(client, api, fake_embed_clock):
    """別々の embed の呼び出しでも、直前 60 秒の量を数える（プロセスの中で共有）。"""
    rag.embed(client, english_pages(60))
    rag.embed(client, english_pages(60))
    tokens = _window_max(api["sent"], lambda x: sum(rag.approx_tokens(t) for t in x["texts"]))
    assert tokens <= rag.EMBED_TOKENS_PER_MIN


# ------------------------------------------------------------------ 429

def test_429_splits_batch_in_half_and_keeps_order(client, api, fake_embed_clock):
    api["reject"] = lambda texts: "Resource exhausted" if len(texts) > 5 else None
    texts = [f"t{i}" for i in range(20)]
    vecs = rag.embed(client, texts)
    assert vecs == [[float(len(t)), 1.0] for t in texts]
    sizes = [len(x["texts"]) for x in api["sent"]]
    assert sizes[0] == 20 and max(sizes[1:]) <= 10 and sizes.count(20) == 1
    ok = [t for x in api["sent"] if len(x["texts"]) <= 5 for t in x["texts"]]
    assert ok == texts                                            # 割っても順は保つ


def test_429_waits_retry_in_seconds(client, api, fake_embed_clock):
    calls = {"n": 0}

    def reject(texts):
        calls["n"] += 1
        return ("Quota exceeded for metric: embed_content_free_tier_tokens, limit: 30000. "
                "Please retry in 37.5s.") if calls["n"] == 1 else None

    api["reject"] = reject
    rag.embed(client, ["only one"])
    assert 38.5 in fake_embed_clock["sleeps"]


def test_single_item_429_forever_raises(client, api, fake_embed_clock):
    api["reject"] = lambda texts: "Resource exhausted"
    with pytest.raises(LLMError) as e:
        rag.embed(client, ["a", "b"])
    assert e.value.status == 429
    assert len(api["sent"]) <= 1 + 1 + rag.EMBED_429_RETRIES + 1   # 2 件 → 1 件ずつ、1 件目で諦める


def test_post_with_retry_429_passthrough(client, monkeypatch):
    """retry_on_429=False なら 429 を待たずに投げる。既定（True）は今までどおり待って再試行する。"""
    replies = [Resp(429, {"error": {"message": "q"}}), Resp(200, {"ok": 1})]
    monkeypatch.setattr(requests, "post", lambda *a, **k: replies.pop(0))
    with pytest.raises(LLMError):
        common._post_with_retry(client, "https://x", {}, retry_on_429=False)
    replies[:] = [Resp(429, {"error": {"message": "q"}}), Resp(200, {"ok": 1})]
    assert common._post_with_retry(client, "https://x", {}) == {"ok": 1}


# ------------------------------------------------------------------ 進み具合

def test_progress_is_reported_per_batch(client, api):
    seen = []
    rag.embed(client, [f"t{i}" for i in range(45)] + ["", "  "], progress=lambda d, n: seen.append((d, n)))
    b = rag.EMBED_BATCH
    expected = [(min(45, k), 45) for k in range(b, 45 + b, b)]
    assert seen == expected                                       # 空の文は数えない


def test_build_passes_progress(client, api):
    seen = []
    pages = [{"lecture": 1, "page": i, "text": f"p{i}"} for i in range(1, 31)]
    index = rag.SlideIndex.build(client, pages, progress=lambda d, n: seen.append(d))
    assert len(index) == 30 and seen[-1] == 30 and len(seen) >= 2
