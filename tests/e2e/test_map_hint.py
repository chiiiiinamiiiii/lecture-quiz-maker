"""地図下の説明が、選んでいるツールに合わせて替わる（#67）。"""
from conftest import tool


def _hint(page) -> str:
    return page.locator(".qm-hint").inner_text()


def test_hint_follows_tool(page):
    assert _hint(page).startswith("ドラッグで移動")            # 最初は手のひら
    assert "クリックで選択" in _hint(page)
    for name in ("lasso", "rect"):
        tool(page, name)
        page.wait_for_timeout(300)
        h = _hint(page)
        assert h.startswith("ドラッグで囲んで選択") and "スペース＋ドラッグで移動" in h
    page.keyboard.press("h")
    page.wait_for_timeout(300)
    assert _hint(page).startswith("ドラッグで移動")


def test_hint_follows_tool_in_english(page):
    page.get_by_text("English", exact=True).click()
    page.wait_for_timeout(3000)
    assert _hint(page).startswith("Drag to pan")
    page.keyboard.press("l")
    page.wait_for_timeout(300)
    assert _hint(page).startswith("Drag to select") and "Space + drag to pan" in _hint(page)
