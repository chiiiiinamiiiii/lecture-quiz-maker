"""止まった通信で長く待たない（Refs #107）。HTTP は差し替え、API は呼ばない。

数学のコースのあとからの索引づくりで、関係の補完に約 480 秒かかった。原因は 2 つ:
- 生成の呼び出しの待ち時間が 300 秒で、応答の返らない 1 本が判定を約 5 分止めた
- Wikipedia の GET が毎回接続し直し、接続できないときは 20 秒待ってその語をあきらめていた
確かめること:
- 生成・埋め込みの呼び出しは (接続 LLM_CONNECT_TIMEOUT, 応答 LLM_TIMEOUT) で送り、時間切れは待って送り直す
- 長くかかった呼び出しは記録に残す
- Wikipedia はスレッドごとに接続を使い回し、接続の失敗・時間切れは 1 度だけやり直す
"""
from __future__ import annotations

import threading

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
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


OK = {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}, "finishReason": "STOP"}]}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(common, "MIN_CALL_INTERVAL", 0.0)
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    return LLMClient(api_key="k", base_url="https://example.invalid/v1beta")


def _call(client):
    return common.call_json(client, model="m", system="s", user="u", schema={"type": "object"})


def test_default_timeout_is_short_enough():
    assert LLMClient(api_key="k").timeout == common.LLM_TIMEOUT
    assert common.LLM_TIMEOUT <= 180                                  # 前は 300 秒
    assert common.LLM_TIMEOUT >= 2 * 62                               # 実測の最長 62 秒の 2 倍はとる


def test_post_uses_connect_and_read_timeout(client, monkeypatch):
    seen = []
    monkeypatch.setattr(requests, "post", lambda url, headers=None, json=None, timeout=None:
                        seen.append(timeout) or Resp(200, OK))
    assert _call(client) == {"ok": True}
    assert seen == [(common.LLM_CONNECT_TIMEOUT, client.timeout)]


def test_read_timeout_is_retried(client, monkeypatch, capsys):
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise requests.ReadTimeout("read timed out")
        return Resp(200, OK)

    monkeypatch.setattr(requests, "post", post)
    assert _call(client) == {"ok": True}
    assert len(calls) == 2
    err = capsys.readouterr().err
    assert "通信エラー ReadTimeout" in err and "打ち切り" in err


def test_slow_call_is_logged(client, monkeypatch, capsys):
    clock = {"t": 0.0}
    monkeypatch.setattr(common.time, "monotonic", lambda: clock["t"])

    def post(*a, **k):
        clock["t"] += 95.0                                            # 応答まで 95 秒
        return Resp(200, OK)

    monkeypatch.setattr(requests, "post", post)
    _call(client)
    assert "[slow] generate の呼び出しに 95 秒" in capsys.readouterr().err


def test_fast_call_is_not_logged(client, monkeypatch, capsys):
    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp(200, OK))
    _call(client)
    assert "[slow]" not in capsys.readouterr().err


# ------------------------------------------------------------------ Wikipedia

def _wiki_payload(params):
    if params.get("list") == "search":
        return {"query": {"search": [{"title": params["srsearch"]}]}}
    return {"query": {"pages": [{"title": params["titles"], "extract": "説明",
                                 "fullurl": "https://en.wikipedia.org/wiki/X"}]}}


def test_wiki_retries_connection_error_once(tmp_path, monkeypatch):
    calls = []

    def get(url, params=None, headers=None, timeout=None):
        calls.append(timeout)
        if len(calls) == 1:
            raise requests.ConnectionError("Connection reset by peer")
        return Resp(200, _wiki_payload(params))

    monkeypatch.setattr(rag, "_http_get", get)
    hit = rag.Wiki(tmp_path, "en").lookup("Regression")
    assert hit and hit["title"] == "Regression"
    assert len(calls) == 3                                            # 失敗 1 + 検索 1 + 本文 1
    assert calls[0] == (rag.WIKI_CONNECT_TIMEOUT, rag.WIKI_TIMEOUT)


def test_wiki_gives_up_after_retry(tmp_path, monkeypatch, capsys):
    calls = []

    def get(url, params=None, headers=None, timeout=None):
        calls.append(1)
        raise requests.ConnectTimeout("timed out")

    monkeypatch.setattr(rag, "_http_get", get)
    assert rag.Wiki(tmp_path, "en").lookup("Regression") is None       # 取り込みは止めない
    assert len(calls) == rag.WIKI_RETRIES + 1
    assert "Wikipedia の検索に失敗" in capsys.readouterr().err


def test_wiki_http_error_is_not_retried(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(rag, "_http_get", lambda *a, **k: calls.append(1) or Resp(503, {}))
    assert rag.Wiki(tmp_path, "en").lookup("Regression") is None
    assert len(calls) == 1


def test_http_get_reuses_session_per_thread(monkeypatch):
    made = []

    class FakeSession:
        def __init__(self):
            made.append(self)

        def get(self, url, **kw):
            return (self, url)

    monkeypatch.setattr(requests, "Session", FakeSession)
    monkeypatch.setattr(rag, "_http_local", threading.local())
    a = rag._http_get("u1")[0]
    b = rag._http_get("u2")[0]
    other = []
    t = threading.Thread(target=lambda: other.append(rag._http_get("u3")[0]))
    t.start()
    t.join()
    assert a is b                                                     # 同じスレッドでは使い回す
    assert other[0] is not a and len(made) == 2                      # スレッドごとに 1 つ
