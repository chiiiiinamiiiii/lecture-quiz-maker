"""common の呼び出しの間隔と 429 の扱い（#107）。HTTP と時計は差し替え、API は呼ばない。

- 生成の呼び出し間隔は既定 4 秒。429 で 1.5 倍ずつ延ばし（上限 12 秒）、しばらく 429 が無ければ戻す
- 埋め込みは生成と別の枠なので、生成の間隔を待たない
- 1 日あたりの枠の 429 は、待たずにすぐ DailyQuotaError（埋め込みでも生成でも）
- 429 の本文に quota の名前（details の quotaId）も入れる
"""
from __future__ import annotations

import pytest
import requests

from quizmap import common, rag
from quizmap.common import DailyQuotaError, LLMClient, LLMError

DAILY = {"error": {"code": 429, "message": "You exceeded your current quota.", "details": [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
        {"quotaMetric": "generativelanguage.googleapis.com/embed_content_free_tier_requests",
         "quotaId": "EmbedContentRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}
PER_MINUTE = {"error": {"code": 429, "message": "Quota exceeded. Please retry in 3s.", "details": [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
        {"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
         "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]}]}}


class Resp:
    def __init__(self, status, payload):
        self.status_code, self._p, self.text = status, payload, str(payload)

    def json(self):
        return self._p


@pytest.fixture
def clock(monkeypatch):
    """common.time を偽の時計に。sleep は時計を進めるだけ。"""
    state = {"t": 5000.0, "sleeps": []}

    class T:
        @staticmethod
        def monotonic():
            return state["t"]

        @staticmethod
        def sleep(sec):
            state["sleeps"].append(round(sec, 3))
            state["t"] += sec

    monkeypatch.setattr(common, "time", T)
    monkeypatch.setattr(common.random, "uniform", lambda a, b: 0.0)
    return state


@pytest.fixture
def client():
    return LLMClient(api_key="k", base_url="https://example.invalid/v1beta")


GEN = "https://example.invalid/v1beta/models/m:generateContent"
EMB = "https://example.invalid/v1beta/models/e:batchEmbedContents"


def test_default_interval_is_4_seconds():
    assert common.MIN_CALL_INTERVAL == 4.0 and common.MAX_CALL_INTERVAL == 12.0
    assert common.current_interval() == 4.0


def test_generate_calls_are_spaced_by_interval(client, clock, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp(200, {"ok": 1}))
    for _ in range(3):
        common._post_with_retry(client, GEN, {})
    assert clock["sleeps"] == [4.0, 4.0]


def test_embed_does_not_wait_for_generate_interval(client, clock, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp(200, {"ok": 1}))
    common._post_with_retry(client, GEN, {})
    common._post_with_retry(client, EMB, {})
    common._post_with_retry(client, EMB, {})
    assert clock["sleeps"] == []


def test_429_widens_interval_up_to_12_and_recovers(client, clock, monkeypatch):
    for _ in range(10):
        common._note_429()
    assert common.current_interval() == 12.0                      # 上限
    common._note_success()
    assert common.current_interval() == 12.0                      # まだ 429 の直後
    clock["t"] += common.INTERVAL_RECOVER_AFTER
    common._note_success()
    assert common.current_interval() == pytest.approx(8.0)        # 1 段（1/1.5）戻す
    common._note_success()
    assert common.current_interval() == pytest.approx(8.0)        # 次の段は、また静かな時間を待つ
    for _ in range(5):
        clock["t"] += common.INTERVAL_RECOVER_AFTER
        common._note_success()
    assert common.current_interval() == 4.0                       # 既定より短くはしない


def test_generate_429_widens_interval(client, clock, monkeypatch):
    replies = [Resp(429, PER_MINUTE), Resp(200, {"ok": 1})]
    monkeypatch.setattr(requests, "post", lambda *a, **k: replies.pop(0))
    assert common._post_with_retry(client, GEN, {}) == {"ok": 1}
    assert common.current_interval() == pytest.approx(6.0)


def test_embed_429_does_not_change_generate_interval(client, clock, monkeypatch):
    replies = [Resp(429, PER_MINUTE), Resp(200, {"ok": 1})]
    monkeypatch.setattr(requests, "post", lambda *a, **k: replies.pop(0))
    common._post_with_retry(client, EMB, {})
    assert common.current_interval() == 4.0


@pytest.mark.parametrize("url", [GEN, EMB])
@pytest.mark.parametrize("retry_on_429", [True, False])
def test_daily_quota_429_is_not_retried(client, clock, monkeypatch, url, retry_on_429):
    sent = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: sent.append(1) or Resp(429, DAILY))
    with pytest.raises(DailyQuotaError) as e:
        common._post_with_retry(client, url, {}, retry_on_429=retry_on_429)
    assert len(sent) == 1                                         # 待たない・送り直さない
    assert all(s < 5 for s in clock["sleeps"])
    assert "PerDay" in str(e.value) and e.value.status == 429


def test_error_message_includes_quota_names():
    msg = common._error_message(Resp(429, DAILY))
    assert "You exceeded" in msg and "EmbedContentRequestsPerDayPerProjectPerModel-FreeTier" in msg
    assert common.is_daily_quota(msg)
    assert not common.is_daily_quota(common._error_message(Resp(429, PER_MINUTE)))
    assert common._error_message(Resp(500, "not a dict")) == "not a dict"


@pytest.mark.parametrize("text,daily", [
    ("Quota exceeded for metric: embed_content_free_tier_requests [quota: X-RequestsPerDayPerProject]", True),
    ("requests per day exceeded", True),
    ("Please retry in 37s. tokens per minute", False),
])
def test_is_daily_quota(text, daily):
    assert common.is_daily_quota(text) is daily


def test_embed_daily_quota_is_not_split(client, monkeypatch):
    """埋め込みの日ごとの枠は、バッチを割っても通らないので、割らずにすぐ投げる。"""
    monkeypatch.setattr(common, "MIN_CALL_INTERVAL", 0.0)
    sent = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: sent.append(1) or Resp(429, DAILY))
    with pytest.raises(DailyQuotaError):
        rag.embed(client, [f"t{i}" for i in range(20)])
    assert len(sent) == 1
