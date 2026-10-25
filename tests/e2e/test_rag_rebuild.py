"""取り込み済みで索引の無いコースに「検索の索引を作る」ボタンを出す（#104 のあとから作る索引）。API は呼ばない。"""
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from conftest import exception_text, main_text

BTN = "検索の索引を作る"


def _make_course(root: Path, index: bool) -> Path:
    d = root / "courses" / "c-test"
    (d / "uploads").mkdir(parents=True)
    (d / "uploads" / "L1.pdf").write_bytes(b"%PDF-1.4 x")
    (d / "course.json").write_text(json.dumps({"name": "テスト", "files": [{"name": "L1.pdf", "lecture": 1, "use": True}]}),
                                   encoding="utf-8")
    for name in ("graph.json", "pages.json", "vocab.json"):
        (d / name).write_text("{}", encoding="utf-8")
    if index:
        (d / "rag_index.json").write_text("{}", encoding="utf-8")
    return d


def test_needs_rag_index(tmp_path, monkeypatch):
    from ui.ingest_view import needs_rag_index
    monkeypatch.delenv("QUIZMAP_RAG", raising=False)
    d = _make_course(tmp_path, index=False)
    assert needs_rag_index(d) is True
    monkeypatch.setenv("QUIZMAP_RAG", "off")
    assert needs_rag_index(d) is False                       # RAG を切っているときは出さない
    monkeypatch.delenv("QUIZMAP_RAG")
    (d / "rag_index.json").write_text("{}", encoding="utf-8")
    assert needs_rag_index(d) is False                       # 索引があるコースには出さない
    (d / "rag_index.json").unlink()
    (d / "graph.json").unlink()
    assert needs_rag_index(d) is False                       # 取り込みが済んでいないコースには出さない（取り込みのボタンを使う）


def _run_page(d: Path) -> AppTest:
    def script(course_dir):
        from pathlib import Path
        from ui.ingest_view import render_ingest_page
        render_ingest_page(Path(course_dir))
    return AppTest.from_function(script, args=(str(d),), default_timeout=30).run()


def _buttons(at: AppTest) -> list[str]:
    return [b.label for b in at.button]


def test_button_calls_start_rag_rebuild(tmp_path, monkeypatch):
    import ui.ingest_view as iv
    monkeypatch.delenv("QUIZMAP_RAG", raising=False)
    calls = []
    monkeypatch.setattr(iv, "start_rag_rebuild", lambda d: calls.append(Path(d)))
    d = _make_course(tmp_path, index=False)
    at = _run_page(d)
    assert not at.exception
    assert BTN in _buttons(at)
    next(b for b in at.button if b.label == BTN).click().run()
    assert calls == [d]


def test_button_shows_error_from_start_rag_rebuild(tmp_path, monkeypatch):
    import ui.ingest_view as iv
    monkeypatch.delenv("QUIZMAP_RAG", raising=False)

    def busy(d):
        raise ValueError("別の取り込み処理が実行中です。")

    monkeypatch.setattr(iv, "start_rag_rebuild", busy)
    at = _run_page(_make_course(tmp_path, index=False))
    next(b for b in at.button if b.label == BTN).click().run()
    assert any("別の取り込み処理が実行中です" in e.value for e in at.error)


@pytest.mark.parametrize("index, rag, shown", [(True, "on", False), (False, "off", False)])
def test_button_hidden(tmp_path, monkeypatch, index, rag, shown):
    monkeypatch.setenv("QUIZMAP_RAG", rag)
    at = _run_page(_make_course(tmp_path, index=index))
    assert not at.exception
    assert (BTN in _buttons(at)) is shown


def test_button_in_english(tmp_path, monkeypatch):
    monkeypatch.delenv("QUIZMAP_RAG", raising=False)
    from ui.i18n import TEXTS
    assert TEXTS["rag_rebuild_btn"]["en"] == "Build the search index"
    assert TEXTS["rag_rebuild_help"]["en"]


def test_button_on_real_ingest_page(page, data_dir):
    """本物の画面（取り込み済みのコースのコピー）でも、索引の有無に合わせて出る。押さない（API を呼ぶ）。"""
    course = next((data_dir / "courses").iterdir())
    page.get_by_text("資料の取り込み", exact=True).click()
    page.wait_for_selector("text=入れた PDF はすべて使います", timeout=15000)
    assert exception_text(page) is None
    has_index = (course / "rag_index.json").exists()
    assert (page.get_by_role("button", name=BTN).count() == 1) is (not has_index)
    assert (("索引がありません" in main_text(page)) is (not has_index))
