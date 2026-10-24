"""quizmap.common.write_json: 置き換えで書く（#54）。読む側に書きかけのファイルが見えない。"""
from __future__ import annotations

import json
import os
import stat
import threading

import pytest

from quizmap import common
from quizmap.common import read_json, write_json


def test_writes_and_overwrites(tmp_path):
    p = tmp_path / "sub" / "a.json"
    write_json(p, {"x": "日本語"})
    assert read_json(p) == {"x": "日本語"}
    assert p.read_text(encoding="utf-8").endswith("\n")
    write_json(p, {"x": 2})
    assert read_json(p) == {"x": 2}
    assert [f.name for f in p.parent.iterdir()] == ["a.json"]        # 一時ファイルが残らない


def test_permissions_like_a_normal_file(tmp_path):
    normal = tmp_path / "normal.txt"
    normal.write_text("x")
    p = tmp_path / "a.json"
    write_json(p, {})
    assert stat.S_IMODE(p.stat().st_mode) == stat.S_IMODE(normal.stat().st_mode)


def test_failed_write_keeps_old_file_and_leaves_no_tmp(tmp_path):
    p = tmp_path / "a.json"
    write_json(p, {"ok": True})
    with pytest.raises(TypeError):
        write_json(p, {"bad": object()})                 # JSON にできない → 書く前に失敗
    assert read_json(p) == {"ok": True}

    def boom(src, dst):
        raise OSError("replace に失敗")
    orig = os.replace
    os.replace = boom
    try:
        with pytest.raises(OSError):
            write_json(p, {"ok": False})
    finally:
        os.replace = orig
    assert read_json(p) == {"ok": True}
    assert [f.name for f in tmp_path.iterdir()] == ["a.json"]


def test_reader_never_sees_partial_file(tmp_path):
    """書き手 4 本と読み手 1 本を同時に回す。読み手は毎回、完全な JSON を読めること。"""
    p = tmp_path / "job.json"
    big = {"log": ["行" * 200] * 200}
    write_json(p, {**big, "n": -1})
    stop = threading.Event()
    errors: list[str] = []

    def writer(k):
        i = 0
        while not stop.is_set():
            write_json(p, {**big, "n": k * 100000 + i})
            i += 1

    def reader():
        for _ in range(3000):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                assert len(data["log"]) == 200
            except Exception as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}"[:200])
        stop.set()

    ws = [threading.Thread(target=writer, args=(k,)) for k in range(4)]
    r = threading.Thread(target=reader)
    for t in ws + [r]:
        t.start()
    r.join(60)
    stop.set()
    for t in ws:
        t.join(10)
    assert errors == []
    assert [f.name for f in tmp_path.iterdir()] == ["job.json"]
