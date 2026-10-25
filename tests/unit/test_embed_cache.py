"""埋め込みのキャッシュ（#107 のつづき）。HTTP は差し替え、API は呼ばない。

同じ資料を何度も取り込み直すと、同じページを毎回埋め込んで 1 日の枠（1000 件）を使い切った。確かめること:
- 一度埋め込んだ文は、コースをまたいで API を呼ばずに使う（取り込み直しの索引づくりは 0 回）
- 途中で失敗しても、通ったバッチの分は残り、次は残りの文だけ送る
- キーは モデル・task・本文（EMBED_MAX_CHARS で切った後）
- 壊れたキャッシュは使わずに埋め込み直す。書けなくても止まらない
キャッシュの置き場は conftest の embed_cache_dir（テストごとの一時フォルダ）。
"""
from __future__ import annotations

import pytest
import requests

from quizmap import common, courses, rag
from quizmap.common import DailyQuotaError, LLMClient


class Resp:
    def __init__(self, status, payload):
        self.status_code, self._p, self.text = status, payload, str(payload)

    def json(self):
        return self._p


DAILY = {"error": {"message": "Quota exceeded for metric: embed_content_free_tier_requests",
                   "details": [{"violations": [{"quotaId": "EmbedContentRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(common, "MIN_CALL_INTERVAL", 0.0)
    return LLMClient(api_key="k", base_url="https://example.invalid/v1beta")


@pytest.fixture
def api(monkeypatch):
    """送った文を記録する偽物。fail_after に数を入れると、その数のバッチの後は日ごとの枠の 429。"""
    state = {"batches": [], "fail_after": None}

    def post(url, headers=None, json=None, timeout=None):
        if state["fail_after"] is not None and len(state["batches"]) >= state["fail_after"]:
            return Resp(429, DAILY)
        texts = [r["content"]["parts"][0]["text"] for r in json["requests"]]
        state["batches"].append(texts)
        return Resp(200, {"embeddings": [{"values": [float(len(t)), 0.5, -2.0]} for t in texts]})

    monkeypatch.setattr(requests, "post", post)
    return state


def sent(api):
    return [t for b in api["batches"] for t in b]


def test_second_embed_uses_cache(client, api):
    texts = [f"page {i}" for i in range(30)]
    first = rag.embed(client, texts)
    n = len(api["batches"])
    again = rag.embed(client, texts)
    assert len(api["batches"]) == n                                # API 0 回
    assert again == first


def test_cache_is_shared_across_courses(client, api, tmp_path):
    pages = [{"lecture": 1, "page": i, "text": f"本文 {i}"} for i in range(1, 41)]
    a = rag.SlideIndex.build(client, pages)
    a.save(tmp_path / "course_a")
    n = len(api["batches"])
    b = rag.SlideIndex.build(client, pages)                        # 別のコースで同じ資料を取り込み直す
    assert len(api["batches"]) == n
    assert (a.vectors == b.vectors).all()


def test_partial_failure_keeps_done_batches(client, api):
    texts = [f"t{i:03d}" for i in range(100)]
    api["fail_after"] = 2                                          # 2 バッチ通ったところで 1 日の枠
    with pytest.raises(DailyQuotaError):
        rag.embed(client, texts)
    done = sent(api)
    assert len(done) == 2 * rag.EMBED_BATCH
    api["fail_after"] = None                                        # 翌日、押し直す
    api["batches"].clear()
    vecs = rag.embed(client, texts)
    assert sent(api) == [t for t in texts if t not in done]        # 残りの文だけ送る
    assert vecs == [[float(len(t)), 0.5, -2.0] for t in texts]


def test_progress_counts_cached(client, api):
    texts = [f"t{i}" for i in range(50)]
    rag.embed(client, texts[:30])
    seen = []
    rag.embed(client, texts, progress=lambda d, n: seen.append((d, n)))
    assert seen[0] == (30, 50) and seen[-1] == (50, 50)


def test_key_includes_task_and_model(client, api, monkeypatch):
    rag.embed(client, ["カント"], task="RETRIEVAL_DOCUMENT")
    rag.embed(client, ["カント"], task="RETRIEVAL_QUERY")
    assert len(api["batches"]) == 2                                 # task が違えば別
    monkeypatch.setattr(rag, "EMBED_MODEL", "other-embedding")
    rag.embed(client, ["カント"], task="RETRIEVAL_DOCUMENT")
    assert len(api["batches"]) == 3                                 # モデルが違えば別


def test_key_uses_truncated_text(client, api):
    base = "x" * rag.EMBED_MAX_CHARS
    rag.embed(client, [base + "A"])
    rag.embed(client, [base + "B"])                                 # 切った後は同じ本文
    assert len(api["batches"]) == 1


def test_broken_cache_file_is_ignored(client, api, embed_cache_dir):
    rag.embed(client, ["倫理"])
    files = list(embed_cache_dir.rglob("*.f32"))
    assert len(files) == 1 and files[0].stat().st_size == 3 * 4
    files[0].write_bytes(b"\x00\x01")                               # 4 の倍数でない
    assert rag.embed(client, ["倫理"]) == [[2.0, 0.5, -2.0]]
    assert len(api["batches"]) == 2


def test_unwritable_cache_does_not_stop(client, api, monkeypatch, tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    monkeypatch.setattr(rag, "EMBED_CACHE_DIR", blocker / "cache")   # ファイルの下には作れない
    assert rag.embed(client, ["a", "b"]) == [[1.0, 0.5, -2.0], [1.0, 0.5, -2.0]]


def test_empty_text_not_cached_or_sent(client, api, embed_cache_dir):
    assert rag.embed(client, ["", "  "]) == [[], []]
    assert api["batches"] == []
    assert not embed_cache_dir.exists() or not list(embed_cache_dir.rglob("*.f32"))


def test_cache_lives_in_data_dir_by_default(tmp_path):
    """既定の置き場は DATA_DIR/embed_cache（QUIZMAP_DATA_DIR に従う）。別のプロセスで読み込んで確かめる。"""
    import os
    import subprocess
    import sys

    env = dict(os.environ, QUIZMAP_DATA_DIR=str(tmp_path))
    env.pop("QUIZMAP_EMBED_CACHE_DIR", None)
    out = subprocess.run([sys.executable, "-c", "from quizmap import rag; print(rag.EMBED_CACHE_DIR)"],
                         env=env, capture_output=True, text=True, check=True,
                         cwd=str(__import__("pathlib").Path(__file__).resolve().parents[2])).stdout.strip()
    assert out == str(tmp_path / "embed_cache")


def test_quota_reset_time_in_jst():
    assert courses.quota_reset_jst() in ("16 時", "17 時")         # 夏時間 16 時、冬時間 17 時
