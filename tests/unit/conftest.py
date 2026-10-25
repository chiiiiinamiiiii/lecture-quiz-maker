"""単体テストの共通部品。API は呼ばない。データは一時フォルダに置き、本物の data/ は触らない。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[2]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from quizmap import courses  # noqa: E402


@pytest.fixture
def courses_dir(tmp_path, monkeypatch):
    """COURSES_DIR を一時フォルダに差し替え、実行中の Job の表も空にする。"""
    d = tmp_path / "courses"
    monkeypatch.setattr(courses, "COURSES_DIR", d)
    monkeypatch.setattr(courses, "_jobs", {})
    return d


class _TickingDatetime(__import__("datetime").datetime):
    """now() を呼ぶたびに 1 秒進む時計。コースの id（秒単位）が重ならないようにする。"""
    _t = None

    @classmethod
    def now(cls, tz=None):
        import datetime as _dt
        cls._t = (cls._t or cls(2026, 10, 24, 9, 0, 0)) + _dt.timedelta(seconds=1)
        return cls._t


@pytest.fixture
def ticking_clock(monkeypatch):
    import types
    _TickingDatetime._t = None
    monkeypatch.setattr(courses, "dt", types.SimpleNamespace(datetime=_TickingDatetime))


@pytest.fixture(autouse=True)
def fake_embed_clock(monkeypatch):
    """rag.embed の 1 分あたりの量の待機を、本当には眠らない偽の時計にする。sleeps に待った秒数が残る。"""
    from quizmap import rag

    clock = {"t": 1000.0, "sleeps": []}

    def sleep(sec):
        clock["sleeps"].append(sec)
        clock["t"] += sec

    monkeypatch.setattr(rag, "_now", lambda: clock["t"])
    monkeypatch.setattr(rag, "_sleep", sleep)
    monkeypatch.setattr(rag, "_sent", __import__("collections").deque())
    return clock
