"""「講義回」の配置の列の見出しの戻り確認（#44）。

見出しは canvas に描くので、map.js が data-axis に出す位置（地図の中の座標）で確かめる。
"""
import json

import pytest

from conftest import app, canvas_center


def _timeline(page):
    page.locator(".qm-toolbar button[data-layout='timeline']").click()
    page.wait_for_timeout(1500)


def _axis(page) -> dict:
    raw = app(page, "a.el.dataset.axis")
    assert raw, "見出しが描かれていない"
    return json.loads(raw)


def _box(page, sel) -> dict:
    """地図の中の座標での要素の位置。"""
    return app(page, f"""(() => {{ const m = a.el.getBoundingClientRect(), r = a.root.querySelector({json.dumps(sel)}).getBoundingClientRect();
        return {{l: r.left - m.left, r: r.right - m.left, t: r.top - m.top, b: r.bottom - m.top}}; }})()""")


def _label_boxes(axis):
    return [{"l": x["x"] - x["w"] / 2, "r": x["x"] + x["w"] / 2, "t": x["y"], "b": x["y"] + x["h"], "n": x["n"]}
            for x in axis["labels"]]


def _overlap(a, b) -> bool:
    return a["l"] < b["r"] and b["l"] < a["r"] and a["t"] < b["b"] and b["t"] < a["b"]


def _assert_readable(page):
    axis = _axis(page)
    labels = _label_boxes(axis)
    assert labels
    for other in (".qm-toolbar", ".qm-lectures", ".qm-band"):
        box = _box(page, other)
        assert not any(_overlap(lb, box) for lb in labels), f"見出しが {other} と重なる"
    for a, b in zip(labels, labels[1:]):
        assert a["r"] <= b["l"], f"見出しどうしが重なる: {a['n']} と {b['n']}"
    return axis


@pytest.mark.parametrize("size", [(1440, 800), (1100, 560)], ids=lambda s: f"{s[0]}x{s[1]}")
def test_axis_labels_at_top_without_overlap(page, size):
    page.set_viewport_size({"width": size[0], "height": size[1]})
    page.wait_for_timeout(1000)
    _timeline(page)
    axis = _assert_readable(page)
    tb = _box(page, ".qm-toolbar")
    assert all(tb["b"] <= x["y"] <= tb["b"] + 20 for x in axis["labels"]), "見出しがツールバーのすぐ下に無い"


def test_axis_labels_are_thinned_when_zoomed_out(page):
    _timeline(page)
    assert _axis(page)["step"] == 1
    cx, cy = app(page, "[a.w / 2, a.h / 2]")
    app(page, f"(a.zoomAt({cx}, {cy}, 0.25), true)")
    page.wait_for_timeout(500)
    axis = _assert_readable(page)
    assert axis["step"] > 1


def test_axis_labels_follow_pan_and_stay_at_top(page):
    _timeline(page)
    before = {x["n"]: x for x in _axis(page)["labels"]}
    cx, cy = canvas_center(page)
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + 60, cy + 80, steps=6)       # 最初のツールは手のひら
    page.mouse.up()
    page.wait_for_timeout(500)
    after = {x["n"]: x for x in _axis(page)["labels"]}
    common = set(before) & set(after)
    assert common
    for n in common:
        assert after[n]["x"] - before[n]["x"] == pytest.approx(60, abs=1)
        assert after[n]["y"] == before[n]["y"]


def test_no_axis_labels_in_force_layout(page):
    assert not app(page, "a.el.dataset.axis")
