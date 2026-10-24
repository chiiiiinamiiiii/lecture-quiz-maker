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
    ctx = browser.new_context(viewport={"width": 1500, "height": 1000})
    pg = ctx.new_page()
    pg.goto(app_url, wait_until="networkidle")
    pg.wait_for_selector(".js-plotly-plot .scatterlayer .points path", state="attached", timeout=60000)
    pg.wait_for_timeout(2500)
    yield pg
    ctx.close()


# ------------------------------------------------------------------ 操作と観測の部品

def exception_text(pg: Page) -> str | None:
    e = pg.locator("[data-testid='stException']")
    return e.first.inner_text()[:1500] if e.count() else None


def main_text(pg: Page) -> str:
    return pg.locator("[data-testid='stMain']").inner_text(timeout=10000)


def panel_shown(pg: Page) -> bool:
    return "不要なものはチェック" in main_text(pg)


def xrange(pg: Page) -> list[float]:
    return pg.evaluate("() => document.querySelector('.js-plotly-plot')._fullLayout.xaxis.range")


def dragmode(pg: Page) -> str:
    return pg.evaluate("() => document.querySelector('.js-plotly-plot')._fullLayout.dragmode")


def selected_nodes(pg: Page) -> int:
    return pg.evaluate("""() => { const gd=document.querySelector('.js-plotly-plot'); let n=0;
      gd.data.forEach(t=>{ if(t.mode==='markers+text' && Array.isArray(t.selectedpoints)) n+=t.selectedpoints.length; });
      return n; }""")


def plot_center(pg: Page) -> tuple[float, float]:
    b = pg.locator(".js-plotly-plot .nsewdrag").first.bounding_box()
    return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2


def pill(pg: Page, name: str) -> None:
    pg.get_by_role("button", name=name, exact=True).click()
    pg.wait_for_timeout(3500)


def lasso_around_median(pg: Page, r: float = 80) -> None:
    """表示中のノードの中央値のあたりを円く投げ縄で囲む。"""
    pts = pg.locator(".js-plotly-plot .scatterlayer .trace .points path.point")
    boxes = [pts.nth(i).bounding_box() for i in range(pts.count())]
    boxes = [b for b in boxes if b and b["width"] > 6]
    xs = sorted(b["x"] + b["width"] / 2 for b in boxes)
    ys = sorted(b["y"] + b["height"] / 2 for b in boxes)
    cx, cy = xs[len(xs) // 2], ys[len(ys) // 2]
    pg.mouse.move(cx + r, cy)
    pg.mouse.down()
    for k in range(1, 41):
        a = 2 * math.pi * k / 40
        pg.mouse.move(cx + r * math.cos(a), cy + r * math.sin(a), steps=2)
    pg.mouse.up()
    pg.wait_for_timeout(4000)


def hand_pan(pg: Page, dx: int = 150) -> float:
    """スペース＋2本指（ホイール）で横に動かし、表示範囲の移動量を返す。"""
    x, y = plot_center(pg)
    pg.mouse.move(x, y)
    r0 = xrange(pg)
    pg.keyboard.down("Space")
    pg.wait_for_timeout(250)
    pg.mouse.wheel(dx, 0)
    pg.wait_for_timeout(500)
    r1 = xrange(pg)
    pg.keyboard.up("Space")
    pg.wait_for_timeout(400)
    assert abs((r1[1] - r1[0]) - (r0[1] - r0[0])) < 1e-6, "手のひら操作で表示の幅（ズーム）が変わった"
    return r1[0] - r0[0]


def press_home(pg: Page) -> None:
    frame = next(f for f in pg.frames if f != pg.main_frame and "表示を元に戻す" in f.content())
    frame.get_by_text("表示を元に戻す").click()
    pg.wait_for_timeout(800)
