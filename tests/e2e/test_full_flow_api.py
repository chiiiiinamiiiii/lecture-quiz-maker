"""アプリの通しの動作確認（API を呼ぶ）: 新しいコース → 取り込み → 地図 → 講義回で選択 → クイズ → 採点 → 出題済み。

RUN_API_TESTS=1 のときだけ動く（@pytest.mark.api）。1 回の実行で API を 5〜6 回呼ぶ:
  取り込み 3 回（語彙・概念・関係）＋ 生成 2 回（作成・誤答の自己確認）＋ 採点 0〜1 回（記述問題があるときだけ）

このテストは取り込み済みのコースを使わない。空のデータ置き場（一時フォルダ）で別にアプリを起動し、
資料の PDF を 1 つアップロードするところから始める。conftest の app_url / page（取り込み済みのコースをコピーする）は使わない。

環境変数:
  QUIZMAP_E2E_LECTURE_PDF  使う PDF（省略時は倫理学の第6回を下のフォルダから探す）
  QUIZMAP_E2E_LECTURE_DIR  その PDF を探すフォルダ
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
import unicodedata
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from conftest import PROJECT, _free_port, app, exception_text, lecture_button, main_text

LECTURE = 6
DEFAULT_DIR = Path.home() / "Documents" / "ICU" / "geh032_WorldOfEthics"
INGEST_TIMEOUT = 600          # 秒。語彙・概念・関係の 3 回の呼び出しと、呼び出し間の待機、スライド画像
GENERATE_TIMEOUT = 300
COURSE_NAME = "通し確認（第6回）"


def _lecture_pdf() -> Path:
    env = os.environ.get("QUIZMAP_E2E_LECTURE_PDF")
    if env:
        return Path(env)
    folder = Path(os.environ.get("QUIZMAP_E2E_LECTURE_DIR", DEFAULT_DIR))
    if folder.exists():
        for p in sorted(folder.glob("*.pdf")):
            if f"第{LECTURE}回" in unicodedata.normalize("NFKC", p.stem):
                return p
    pytest.skip(f"第{LECTURE}回の PDF が見つからない。QUIZMAP_E2E_LECTURE_PDF で指定すること")


# ------------------------------------------------------------------ 空のデータ置き場で起動するアプリ

@pytest.fixture(scope="module")
def fresh_data_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("quizmap-fresh")


@pytest.fixture(scope="module")
def fresh_app_url(fresh_data_dir):
    port = _free_port()
    env = dict(os.environ, QUIZMAP_DATA_DIR=str(fresh_data_dir),
               QUIZMAP_ENV_FILE=os.environ.get("QUIZMAP_ENV_FILE", str(PROJECT / ".env")))
    log = open(fresh_data_dir / "streamlit.log", "w")
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
        raise RuntimeError(f"アプリが起動しない。ログ: {fresh_data_dir / 'streamlit.log'}")
    yield url
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture
def fresh_page(browser, fresh_app_url) -> Page:
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)[:300]))
    pg.goto(fresh_app_url, wait_until="networkidle")
    yield pg
    ctx.close()


# ------------------------------------------------------------------ 部品

def _no_exception(pg: Page, where: str) -> None:
    err = exception_text(pg)
    assert err is None, f"{where} で例外: {err}"


def _wait_text(pg: Page, text: str, timeout: float, where: str) -> None:
    """メイン領域に text が出るまで待つ。途中で例外が出たらすぐ失敗にする。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        _no_exception(pg, where)
        if text in main_text(pg):
            return
        pg.wait_for_timeout(1000)
    raise AssertionError(f"{where}: {timeout:.0f} 秒待っても「{text}」が出ない。画面:\n{main_text(pg)[-1500:]}")


def _course_dir(data_dir: Path) -> Path:
    dirs = [d for d in (data_dir / "courses").glob("*") if (d / "course.json").exists()]
    assert len(dirs) == 1, f"コースが 1 つだけ作られるはず: {dirs}"
    return dirs[0]


def create_course_until_ingest(pg: Page, data_dir: Path, pdf: Path) -> Path:
    """新しいコースを作り、講義回の割り当てを確かめ、「取り込みを始める」が押せる所まで進む。API は呼ばない。"""
    main = pg.locator("[data-testid='stMain']")
    _wait_text(pg, "新しいコースを作る", 30, "新しいコースの画面")
    main.get_by_label("コース名").fill(COURSE_NAME)
    main.get_by_label("コース名").press("Enter")
    main.locator("[data-testid='stFileUploader'] input[type='file']").first.set_input_files(str(pdf))
    expect(main.get_by_role("button", name=f"Remove {pdf.name}")).to_be_visible(timeout=20000)   # 表示名は省略されるので
    create = main.get_by_role("button", name="コースを作る")
    expect(create).to_be_enabled(timeout=10000)
    create.click()

    _wait_text(pg, "講義回の割り当て", 30, "コースを作った直後")
    d = _course_dir(data_dir)
    info = json.loads((d / "course.json").read_text(encoding="utf-8"))
    assert info["name"] == COURSE_NAME
    assert [(f["lecture"], f["use"]) for f in info["files"]] == [(LECTURE, True)], info["files"]
    assert (d / "uploads" / info["files"][0]["name"]).read_bytes() == pdf.read_bytes()
    _wait_text(pg, "1 回分を取り込みます", 15, "講義回の割り当て")
    expect(main.get_by_role("button", name="取り込みを始める")).to_be_enabled(timeout=10000)
    return d


# ------------------------------------------------------------------ テスト

@pytest.mark.api
def test_full_flow_from_upload_to_asked_on_map(fresh_page, fresh_data_dir):
    pg = fresh_page
    main = pg.locator("[data-testid='stMain']")
    d = create_course_until_ingest(pg, fresh_data_dir, _lecture_pdf())

    # 取り込み → 進捗 → 完了後に地図
    main.get_by_role("button", name="取り込みを始める").click()
    pg.wait_for_timeout(3000)
    _no_exception(pg, "取り込みを始めた直後")
    seen_progress = main.locator("[data-testid='stProgress']").count() > 0 or \
        pg.locator("[data-testid='stSidebar'] [data-testid='stProgress']").count() > 0
    end = time.monotonic() + INGEST_TIMEOUT
    while time.monotonic() < end:
        job = json.loads((d / "job.json").read_text(encoding="utf-8")) if (d / "job.json").exists() else {}
        if job.get("status") == "error":
            pytest.fail(f"取り込みが失敗: {job.get('error')}\n" + "\n".join(job.get("log", [])[-15:]))
        if job.get("status") == "done":
            break
        if not seen_progress:
            seen_progress = pg.locator("[data-testid='stProgress']").count() > 0
        pg.wait_for_timeout(2000)
    else:
        pytest.fail(f"{INGEST_TIMEOUT} 秒で取り込みが終わらない")
    assert seen_progress, "取り込み中に進捗バーが出なかった"
    assert (d / "graph.json").exists() and (d / "vocab.json").exists()

    pg.wait_for_function("() => window.__qmMapApp && window.__qmMapApp.view", timeout=60000)
    pg.wait_for_timeout(1500)
    _no_exception(pg, "取り込み完了後の地図")
    graph = json.loads((d / "graph.json").read_text(encoding="utf-8"))
    assert graph["concepts"], "概念が 1 つも抽出されなかった"
    assert app(pg, "a.nodes.length") > 0
    assert app(pg, "a.nodes.filter(n => n.asked).length") == 0

    # 講義回ボタン「第6回」で選択 → 確定
    lecture_button(pg, str(LECTURE))
    in_lecture = {c["id"] for c in graph["concepts"] if LECTURE in c["lectures"]}
    selected = set(app(pg, "[...a.selected]"))
    assert selected and selected <= in_lecture, f"第{LECTURE}回の概念だけが選ばれるはず: {selected - in_lecture}"
    _wait_text(pg, "この範囲で確定", 15, "講義回で選んだ後")
    main.get_by_role("button", name="この範囲で確定").click()
    _wait_text(pg, f"確定した {len(selected)} 概念", 15, "確定")

    # クイズを 3 問作る
    n_input = main.get_by_label("問題数")
    n_input.fill("3")
    n_input.press("Enter")
    pg.wait_for_timeout(1500)
    main.get_by_role("button", name="クイズを作る").click()
    _wait_text(pg, "採点する", GENERATE_TIMEOUT, "クイズの生成")
    quiz = json.loads((d / "quiz_latest.json").read_text(encoding="utf-8"))
    questions = quiz["questions"]
    assert len(questions) == 3, f"3 問のはずが {len(questions)} 問"
    for q in questions:
        assert set(q["concepts"]) <= selected, f"確定した範囲の外の概念が出題された: {q['concepts']}"

    # 4 択は 1 つ目の選択肢を選び、記述は何か書く → 採点
    for i, q in enumerate(questions, start=1):
        box = main.locator("[data-testid='stVerticalBlockBorderWrapper']").filter(has_text=f"問 {i}").last
        if q["type"] == "mcq":
            assert len(q["choices"]) == 4
            box.locator("[data-testid='stRadio'] label").first.click()
        else:
            box.locator("input[type='text']").fill("わからない")
            box.locator("input[type='text']").press("Enter")
        pg.wait_for_timeout(800)
    main.get_by_role("button", name="採点する").click()

    # 結果と解説が出る
    _wait_text(pg, " / 3 点", GENERATE_TIMEOUT, "採点")
    text = main_text(pg)
    assert text.count("解説:") >= 3, "各問の解説が出ていない"
    assert "正答:" in text or "模範解答:" in text
    results = json.loads((d / "results.json").read_text(encoding="utf-8"))
    assert len(results["attempts"]) == 3
    asked = set(results["asked_concepts"])
    assert asked and asked <= selected

    # 地図の該当の概念が出題済みになる
    pg.wait_for_function(
        "(ids) => { const a = window.__qmMapApp; return a && ids.every(i => a.nodes.some(n => n.id === i && n.asked)); }",
        arg=sorted(asked), timeout=30000)
    on_map = set(app(pg, "a.nodes.filter(n => n.asked).map(n => n.id)"))
    assert on_map == asked, f"地図の出題済み {sorted(on_map)} と results.json {sorted(asked)} が違う"
    _no_exception(pg, "採点後")
    assert not pg.errors, f"ブラウザでエラー: {pg.errors}"
