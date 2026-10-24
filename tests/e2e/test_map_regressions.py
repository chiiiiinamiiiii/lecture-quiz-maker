"""地図・範囲選択・右側パネルの戻り確認（API を呼ばない）。

UI を変えたら、取り込む前に必ず全部通すこと:
    dev/py.sh -m pytest tests/e2e -q
"""
import json

from conftest import (
    app, canvas_center, exception_text, lasso, lecture_button, main_text, n_selected, node_xy, panel_count,
    tool, view,
)


def lecture_concepts(graph, n):
    return {c["id"] for c in graph["concepts"] if n in c["lectures"]}


def test_map_loads(page, graph):
    assert exception_text(page) is None
    assert page.errors == []
    assert app(page, "a.nodes.length") == len(graph["concepts"])
    assert page.locator(".qm-toolbar button[data-tool]").count() == 3


def test_no_kind_shapes_only_two_states(page):
    """種類（形）の区別は廃止。凡例は 未出題 / 出題済み / 選択中 の 3 つだけ。"""
    assert page.locator(".qm-legend span").count() == 3


def test_lasso_replace_add_subtract(page):
    x, y, _ = node_xy(page, 0)
    lasso(page, x, y, 60)
    first = n_selected(page)
    assert first > 0 and panel_count(page) == first
    x2, y2, _ = node_xy(page, 10)
    lasso(page, x2, y2, 40, "Shift")
    added = n_selected(page)
    assert added >= first
    lasso(page, x, y, 60, "Meta")
    assert n_selected(page) < added
    assert panel_count(page) in (None, n_selected(page))


def test_lecture_buttons_follow_modifier_rules(page, graph):
    l3, l12 = lecture_concepts(graph, 3), lecture_concepts(graph, 12)
    lecture_button(page, "3")
    assert n_selected(page) == len(l3) and panel_count(page) == len(l3)
    lecture_button(page, "12", "Shift")
    assert n_selected(page) == len(l3 | l12)
    lecture_button(page, "3", "Meta")
    assert n_selected(page) == len(l12 - l3)
    lecture_button(page, "all")
    assert n_selected(page) == len(graph["concepts"])


def test_hand_tool_drag_pans_and_keeps_selection(page):
    lecture_button(page, "3")
    tool(page, "hand")
    cx, cy = canvas_center(page)
    v0 = view(page)
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + 120, cy + 40, steps=8)
    page.mouse.up()
    v1 = view(page)
    assert round(v1["tx"] - v0["tx"]) == 120 and round(v1["ty"] - v0["ty"]) == 40
    assert v1["scale"] == v0["scale"]
    assert n_selected(page) > 0


def test_two_finger_zooms_and_space_pans(page):
    cx, cy = canvas_center(page)
    page.mouse.move(cx, cy)
    s0 = view(page)["scale"]
    page.mouse.wheel(0, -200)
    page.wait_for_timeout(300)
    assert view(page)["scale"] > s0
    v = view(page)
    page.keyboard.down("Space")
    page.mouse.wheel(80, 0)
    page.mouse.down()
    page.mouse.move(cx - 100, cy, steps=8)
    page.mouse.up()
    page.keyboard.up("Space")
    v2 = view(page)
    assert round(v2["tx"] - v["tx"]) == -180 and v2["scale"] == v["scale"]
    assert app(page, "a.tool") == "lasso"


def test_space_after_lecture_button_does_not_toggle_it(page):
    """ボタンにフォーカスが残った状態でスペースを押しても、そのボタンが押されない（過去に壊れた）。"""
    lecture_button(page, "3")
    n = n_selected(page)
    page.keyboard.down("Space")
    page.keyboard.up("Space")
    page.wait_for_timeout(1500)
    assert n_selected(page) == n
    assert app(page, "[...a.activeLectures]") == ["3"]


def test_view_and_selection_survive_reruns(page):
    lecture_button(page, "3")
    tool(page, "hand")
    cx, cy = canvas_center(page)
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + 50, cy, steps=5)
    page.mouse.up()
    v = view(page)
    page.get_by_role("button", name="この範囲で確定").click()
    page.wait_for_timeout(2500)
    assert "確定した" in main_text(page)
    assert view(page) == v
    assert n_selected(page) > 0


def test_timeline_lasso_does_not_pull_far_concepts_via_edges(page):
    """講義回の配置では辺の中点で選ばない（#17）。"""
    page.locator(".qm-toolbar button[data-layout='timeline']").click()
    page.wait_for_timeout(1500)
    x, y, _ = node_xy(page, 0)
    lasso(page, x, y, 80)
    assert app(page, "a.viaEdge.size") == 0


def test_escape_clears_selection(page):
    lecture_button(page, "3")
    page.keyboard.press("Escape")
    page.wait_for_timeout(2000)
    assert n_selected(page) == 0
    assert panel_count(page) is None


def test_space_in_text_input_types_a_space(page):
    inp = page.locator("[data-testid='stSidebar'] [data-testid='stTextInputField']").first
    inp.click()
    inp.press("End")
    page.keyboard.press("Space")
    assert inp.input_value().endswith(" ")
    assert app(page, "a.space") is False


def test_language_switch_keeps_selection(page):
    lecture_button(page, "3")
    n = n_selected(page)
    page.get_by_text("English", exact=True).click()
    page.wait_for_timeout(3000)
    assert "Selection" in main_text(page)
    assert page.locator(".qm-lectures button[data-lec='1']").inner_text() == "L1"
    assert n_selected(page) == n


def test_asked_concepts_follow_results_file(page, data_dir, graph):
    """results.json が更新されたら、読み直して出題済みを地図に出す（#14: キャッシュが古いままだった）。"""
    course = next((data_dir / "courses").iterdir())
    target = graph["concepts"][0]["id"]
    (course / "results.json").write_text(json.dumps({"attempts": [], "asked_concepts": [target]}), encoding="utf-8")
    page.reload(wait_until="networkidle")
    page.wait_for_function("() => window.__qmMapApp && window.__qmMapApp.view", timeout=60000)
    assert app(page, f"a.byId.get({json.dumps(target)}).asked") is True
    (course / "results.json").unlink()


def test_ingest_page_opens(page):
    page.get_by_text("資料の取り込み", exact=True).click()
    page.wait_for_timeout(2500)
    assert exception_text(page) is None
    assert "講義回の割り当て" in main_text(page)
