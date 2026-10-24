"""これまでに戻り（一度直したのにまた壊れる）が出た挙動の確認リスト。

UI を変えたら、取り込む前に必ず全部通すこと:
    .venv/bin/python -m pytest tests/e2e -q
"""
from conftest import (
    dragmode, exception_text, hand_pan, lasso_around_median, main_text, panel_shown, pill, plot_center,
    press_home, selected_nodes, xrange,
)

COLOR_UNASKED = "#2E1A47"


def lecture_concepts(graph, n):
    return {c["id"] for c in graph["concepts"] if n in c["lectures"]}


def test_map_loads_without_error(page):
    assert exception_text(page) is None
    assert page.locator(".js-plotly-plot").count() == 1


def test_x_axis_shows_only_real_lectures(page, graph):
    lectures = sorted({n for c in graph["concepts"] for n in c["lectures"]})
    ticks = page.evaluate("() => Array.from(document.querySelectorAll('.js-plotly-plot .xaxislayer-above .xtick text'))"
                          ".map(t => t.getAttribute('data-unformatted'))")
    assert ticks == [f"第{n}回" for n in lectures]
    assert [round(v, 2) for v in xrange(page)] == [min(lectures) - 0.5, max(lectures) + 0.5]


def test_modebar_has_only_selection_tools(page):
    page.hover(".js-plotly-plot")
    page.wait_for_timeout(300)
    btns = page.locator(".js-plotly-plot .modebar-btn")
    titles = [btns.nth(i).get_attribute("data-title") for i in range(btns.count())]
    assert titles == ["Box Select", "Lasso Select", "Fullscreen"]


def test_lecture_button_alone_selects_like_lasso(page, graph):
    """講義回ボタンだけで（投げ縄を使わずに）選択パネルが出て、地図上も投げ縄と同じ表現になる。"""
    pill(page, "第3回")
    expected = lecture_concepts(graph, 3)
    assert panel_shown(page)
    assert f"囲んだ {len(expected)} 個" in main_text(page)
    assert selected_nodes(page) == len(expected)
    page.get_by_text("この選択で確定").click()
    page.wait_for_timeout(2500)
    assert f"確定した {len(expected)} 概念" in main_text(page)


def test_lecture_buttons_union_and_all(page, graph):
    pill(page, "第3回")
    pill(page, "第12回")
    union = lecture_concepts(graph, 3) | lecture_concepts(graph, 12)
    assert f"囲んだ {len(union)} 個" in main_text(page)
    pill(page, "全部")
    assert f"囲んだ {len(graph['concepts'])} 個" in main_text(page)


def test_lasso_then_lecture_button_keeps_both(page, graph):
    lasso_around_median(page)
    assert panel_shown(page)
    n_lasso = selected_nodes(page)
    assert n_lasso > 0
    pill(page, "第5回")
    assert selected_nodes(page) >= max(n_lasso, len(lecture_concepts(graph, 5)))


def test_reset_clears_everything(page):
    pill(page, "第3回")
    lasso_around_median(page)
    page.get_by_text("選択をリセット").click()
    page.wait_for_timeout(3000)
    assert not panel_shown(page)
    assert selected_nodes(page) == 0
    assert page.locator("[data-testid='stPills'] [aria-checked='true']").count() == 0


def test_hand_tool_works_repeatedly(page):
    """スペース＋2本指の移動が、何度でも・どの操作のあとでも効く（以前は 2 回目が効かなかった）。"""
    assert hand_pan(page) > 0, "1 回目"
    assert hand_pan(page) > 0, "続けて 2 回目"
    pill(page, "第3回")
    assert hand_pan(page) > 0, "再実行（講義回ボタン）のあと"
    press_home(page)
    assert hand_pan(page) > 0, "「表示を元に戻す」のあと"
    x, y = plot_center(page)
    page.mouse.click(x, y)
    page.wait_for_timeout(1500)
    assert hand_pan(page) > 0, "地図をクリックしたあと"
    assert dragmode(page) == "lasso", "スペースを離したらツールが投げ縄に戻る"
    assert panel_shown(page), "手のひら操作で講義回ボタンの選択が外れていない"


def test_wheel_without_space_zooms(page):
    x, y = plot_center(page)
    page.mouse.move(x, y)
    r0 = xrange(page)
    page.mouse.wheel(0, 120)
    page.wait_for_timeout(500)
    r1 = xrange(page)
    assert abs((r1[1] - r1[0]) - (r0[1] - r0[0])) > 0.01


def test_view_is_kept_after_rerun(page):
    hand_pan(page)
    r = xrange(page)
    pill(page, "第3回")
    assert [round(v, 2) for v in xrange(page)] == [round(v, 2) for v in r]
    press_home(page)
    assert xrange(page) != r


def test_space_in_text_input_types_a_space(page):
    inp = page.get_by_test_id("stTextInputField").first
    inp.click()
    inp.press("End")
    page.keyboard.press("Space")
    assert inp.input_value().endswith(" ")
    assert not page.evaluate("() => document.querySelector('.js-plotly-plot').className.includes('quizmap-pan')")


def test_relation_layout_lecture_button(page, graph):
    page.get_by_text("関係ベース（力学的）").click()
    page.wait_for_timeout(3000)
    pill(page, "第3回")
    assert exception_text(page) is None
    assert selected_nodes(page) == len(lecture_concepts(graph, 3))


def test_legend_swatches_keep_fixed_color(page):
    colors = page.evaluate("""() => document.querySelector('.js-plotly-plot').data
        .filter(t => t.legend === 'legend' && t.showlegend !== false && t.name !== '関係（辺の中点）')
        .map(t => t.marker.color)""")
    assert colors and all(c == COLOR_UNASKED for c in colors)


def test_ingest_page_opens(page):
    page.get_by_text("資料の取り込み", exact=True).click()
    page.wait_for_timeout(2500)
    assert exception_text(page) is None
    assert "講義回の割り当て" in main_text(page)
