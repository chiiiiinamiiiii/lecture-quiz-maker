"""ブラウザで実際に操作する E2E テストの共通部品。

- 本物のデータ（data/courses/ の取り込み済みコース）を一時フォルダにコピーし、
  QUIZMAP_DATA_DIR でそこを指してアプリを起動する。本物のデータは触らない
- アプリは空いているポートで 1 回だけ起動し、テストごとに新しいブラウザのページ（= 新しいセッション）を使う

環境変数:
  QUIZMAP_FIXTURE_COURSE  コピー元のコースのフォルダ（省略時は data/courses/ の取り込み済みで最も古いもの）
  RUN_API_TESTS=1         API を呼ぶテスト（@pytest.mark.api）も実行する
"""
from __future__ import annotations

import json
import math
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import Page, sync_playwright

PROJECT = Path(__file__).resolve().parents[2]
REAL_DATA = Path(os.environ.get("QUIZMAP_REAL_DATA_DIR", PROJECT / "data"))
SKIP_FILES = {"results.json", "job.json", "quiz_latest.json"}


def pytest_configure(config):
    config.addinivalue_line("markers", "api: 実際に LLM API を呼ぶテスト（RUN_API_TESTS=1 のときだけ実行）")


def pytest_collection_modifyitems(config, items):
    if os.environ.get("RUN_API_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="API を呼ぶテスト。RUN_API_TESTS=1 で実行する")
    for item in items:
        if "api" in item.keywords:
            item.add_marker(skip)


def _fixture_course() -> Path:
    env = os.environ.get("QUIZMAP_FIXTURE_COURSE")
    if env:
        return Path(env)
    courses = sorted((d for d in (REAL_DATA / "courses").glob("*") if (d / "graph.json").exists()),
                     key=lambda d: json.loads((d / "course.json").read_text(encoding="utf-8")).get("created_at", ""))
    if not courses:
        pytest.skip("取り込み済みのコースが無い（data/courses/）。先にアプリで 1 つ取り込むこと")
    return courses[0]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory) -> Path:
    src = _fixture_course()
    d = tmp_path_factory.mktemp("quizmap-data")
    dst = d / "courses" / src.name
    shutil.copytree(src, dst, ignore=lambda _dir, names: [n for n in names if n in SKIP_FILES])
    return d


@pytest.fixture(scope="session")
def graph(data_dir) -> dict:
    course = next((data_dir / "courses").iterdir())
    return json.loads((course / "graph.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def app_url(data_dir):
    port = _free_port()
    env = dict(os.environ, QUIZMAP_DATA_DIR=str(data_dir),
               QUIZMAP_ENV_FILE=os.environ.get("QUIZMAP_ENV_FILE", str(PROJECT / ".env")))
    log = open(data_dir / "streamlit.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", str(PROJECT / "app.py"), "--server.headless", "true",
         "--server.port", str(port), "--server.runOnSave", "false", "--browser.gatherUsageStats", "false"],
        cwd=PROJECT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}/"
    for _ in range(60):
        try:
            if urllib.request.urlopen(url, timeout=1).status == 200:
                break
        except OSError:
            time.sleep(0.5)
    else:
        proc.terminate()
        raise RuntimeError(f"アプリが起動しない。ログ: {data_dir / 'streamlit.log'}")
    yield url
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser, app_url) -> Page:
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)[:300]))
    pg.goto(app_url, wait_until="networkidle")
    pg.wait_for_function("() => window.__qmMapApp && window.__qmMapApp.view", timeout=60000)
    pg.wait_for_timeout(1500)
    yield pg
    ctx.close()


# ------------------------------------------------------------------ 操作と観測の部品（地図は ui/static/map.js）

def exception_text(pg: Page) -> str | None:
    e = pg.locator("[data-testid='stException']")
    return e.first.inner_text()[:1500] if e.count() else None


def main_text(pg: Page) -> str:
    return pg.locator("[data-testid='stMain']").inner_text(timeout=10000)


def app(pg: Page, expr: str):
    """地図部品の状態を読む（a = MapApp）。"""
    return pg.evaluate(f"() => {{ const a = window.__qmMapApp; return {expr}; }}")


def n_selected(pg: Page) -> int:
    return app(pg, "a.selected.size")


def panel_count(pg: Page) -> int | None:
    """右側パネルに並んだ選択の件数（チェックボックスの数）。"""
    t = main_text(pg)
    if "個（うち" not in t:
        return None
    return pg.locator("[data-testid='stCheckbox']").count()


def node_xy(pg: Page, rank: int) -> tuple[float, float, str]:
    """重みの順位 rank の概念の、ページ上の座標と id。"""
    return tuple(app(pg, f"""(() => {{ const n = a.ranked[{rank}]; const p = a.toScreen(a.pos.get(n.id));
        const r = a.canvas.getBoundingClientRect(); return [p[0] + r.left, p[1] + r.top, n.id]; }})()"""))


def canvas_center(pg: Page) -> tuple[float, float]:
    b = pg.locator("canvas").first.bounding_box()
    return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2


def lasso(pg: Page, cx: float, cy: float, r: float = 60, modifier: str | None = None) -> None:
    """投げ縄ツールで (cx, cy) のまわりを円く囲む。最初のツールは手のひらなので、投げ縄に切り替えてから行う。"""
    if app(pg, "a.tool") != "lasso":
        tool(pg, "lasso")
    if modifier:
        pg.keyboard.down(modifier)
    pg.mouse.move(cx + r, cy)
    pg.mouse.down()
    for k in range(1, 41):
        a = 2 * math.pi * k / 40
        pg.mouse.move(cx + r * math.cos(a), cy + r * math.sin(a), steps=2)
    pg.mouse.up()
    if modifier:
        pg.keyboard.up(modifier)
    pg.wait_for_timeout(2500)


def lecture_button(pg: Page, key: str, modifier: str | None = None) -> None:
    if modifier:
        pg.keyboard.down(modifier)
    pg.locator(f".qm-lectures button[data-lec='{key}']").click()
    if modifier:
        pg.keyboard.up(modifier)
    pg.wait_for_timeout(2500)


def tool(pg: Page, name: str) -> None:
    pg.locator(f".qm-toolbar button[data-tool='{name}']").click()


def view(pg: Page) -> dict:
    return app(pg, "({...a.view})")
