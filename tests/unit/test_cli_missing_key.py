"""CLI はキーが無いとき、案内を表示して終了コード 1 で終わる（#2）。API は呼ばない。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from quizmap import common

PROJECT = Path(__file__).resolve().parents[2]


def test_make_client_raises_instead_of_exit(monkeypatch):
    monkeypatch.setattr(common, "load_dotenv", lambda *a, **kw: None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(common.MissingAPIKeyError, match="GEMINI_API_KEY がない"):
        common.make_client()
    assert issubclass(common.MissingAPIKeyError, RuntimeError)


def _run_cli(tmp_path, *args):
    env = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
    env["QUIZMAP_ENV_FILE"] = str(tmp_path / "no.env")       # 本物の .env を読ませない
    env["QUIZMAP_DATA_DIR"] = str(tmp_path / "data")
    return subprocess.run([sys.executable, *args], cwd=PROJECT, env=env,
                          capture_output=True, text=True, timeout=60)


def test_list_models_without_key(tmp_path):
    r = _run_cli(tmp_path, "list_models.py")
    assert r.returncode == 1
    assert "[error] GEMINI_API_KEY がない" in r.stderr
    assert "Traceback" not in r.stderr


def test_derive_vocab_without_key(tmp_path):
    """PDF の読み込みまで進んでから make_client で止まる。pdftotext を使わないよう、読み込みは差し替える。"""
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    script = (
        "import sys, runpy; sys.argv=['derive_vocab.py', sys.argv[1], '--out', sys.argv[2]];"
        "import quizmap.common as c;"
        "c.load_lectures=lambda d: ([c.Lecture(number=1, path=d/'第1回.pdf', pages=['x'])], []);"
        "runpy.run_path('derive_vocab.py', run_name='__main__')"
    )
    r = _run_cli(tmp_path, "-c", script, str(pdf_dir), str(tmp_path / "vocab.json"))
    assert r.returncode == 1, r.stderr
    assert "[error] GEMINI_API_KEY がない" in r.stderr
    assert "Traceback" not in r.stderr
