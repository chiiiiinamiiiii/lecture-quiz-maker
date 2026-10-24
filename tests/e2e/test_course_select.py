"""コース選択欄の戻り確認（#52, #16）。

コース欄の表示名（言語・「未取り込み」・コース名）が変わっても、選んでいるコースは変わらず、
選択やクイズの状態が消えないこと。閉じた欄の文字が今の表示名になること。
"""
import pytest

from conftest import app, exception_text, lecture_button, n_selected


def lecture_concepts(graph, n):
    return {c["id"] for c in graph["concepts"] if n in c["lectures"]}


def _course_box(page):
    return page.locator("[data-testid='stSidebar'] [data-testid='stSelectbox']").first


def _course_box_text(page) -> str:
    """閉じたコース欄に見えている文字（版によって入力欄の value か、中の文字に出る）。"""
    box = _course_box(page)
    vals = [i.input_value() for i in box.locator("input").all()]
    return " ".join([box.inner_text(), *vals])


def _wait_map(page):
    page.wait_for_function("() => window.__qmMapApp && window.__qmMapApp.view", timeout=30000)
    page.wait_for_timeout(1500)


def test_first_selection_after_language_switch_is_kept(page, graph):
    """言語を切り替えて表示名が変わった直後の最初の選択が、コースの選び直しで消えない（#52 と同じ仕組みの見張り）。"""
    page.get_by_text("English", exact=True).click()
    page.wait_for_timeout(3000)
    _wait_map(page)
    lecture_button(page, "3")
    page.wait_for_timeout(2000)
    assert exception_text(page) is None
    assert n_selected(page) == len(lecture_concepts(graph, 3))
    page.get_by_text("日本語", exact=True).click()
    page.wait_for_timeout(3000)
    assert n_selected(page) == len(lecture_concepts(graph, 3))


def test_first_selection_after_rename_is_kept(page, graph):
    """コース名を変える（表示名が変わる）と、その直後の選択が消えていた（#52 の見込み）。"""
    box = page.locator("[data-testid='stSidebar'] input[aria-label='コース名']").first
    old = box.input_value()
    try:
        box.fill(old + "改")
        box.press("Enter")
        page.wait_for_timeout(3000)
        _wait_map(page)
        assert old + "改" in _course_box_text(page)
        lecture_button(page, "3")
        page.wait_for_timeout(2000)
        assert exception_text(page) is None
        assert n_selected(page) == len(lecture_concepts(graph, 3))
    finally:
        box = page.locator("[data-testid='stSidebar'] input[aria-label='コース名']").first
        box.fill(old)
        box.press("Enter")
        page.wait_for_timeout(2500)


def test_closed_course_box_follows_language(page):
    """#16: 言語を切り替えると、閉じたコース欄の文字も切り替わる（「＋ 新しいコース」で確かめる）。"""
    _course_box(page).click()
    page.get_by_role("option", name="＋ 新しいコース").click()
    page.wait_for_timeout(2500)
    assert "＋ 新しいコース" in _course_box_text(page)
    page.get_by_text("English", exact=True).click()
    page.wait_for_timeout(2500)
    text = _course_box_text(page)
    assert "+ New course" in text and "新しいコース" not in text
    assert "Create a course" in page.locator("[data-testid='stMain']").inner_text()
    page.get_by_text("日本語", exact=True).click()
    page.wait_for_timeout(2500)
    assert "＋ 新しいコース" in _course_box_text(page)


def test_other_relation_label_follows_language(page, graph):
    """関係ラベル「その他」は画面の言語で出す（English なら Other）。"""
    n_other = sum(r["label"] == "その他" for r in graph["relations"])
    if not n_other:
        pytest.skip("このコースには「その他」の関係が無い")
    assert app(page, "a.edges.filter(e => e.label === 'その他').length") == n_other
    page.get_by_text("English", exact=True).click()
    page.wait_for_timeout(3000)
    assert app(page, "a.edges.filter(e => e.label === 'Other').length") == n_other
    assert app(page, "a.edges.filter(e => e.label === 'その他').length") == 0
