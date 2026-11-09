"""関係ベースの地図が、点を置ける範囲の全体に広がる（四隅が大きく空かない）。API は呼ばない。"""
from conftest import app

GRID = 5


def _metrics(pg):
    return app(pg, f"""(() => {{ const m = a._fitMargins(), P = [...a.visibleIds()].map((id) => a.toScreen(a.pos.get(id)));
        const x0 = 48, y0 = m.top, W = a.w - 96, H = a.h - m.top - m.bottom, cells = new Set();
        for (const [x, y] of P) cells.add(Math.min({GRID - 1}, Math.floor((x - x0) / W * {GRID})) + "," + Math.min({GRID - 1}, Math.floor((y - y0) / H * {GRID})));
        const xs = P.map((p) => p[0]), ys = P.map((p) => p[1]);
        return {{ w: (Math.max(...xs) - Math.min(...xs)) / W, h: (Math.max(...ys) - Math.min(...ys)) / H, cells: cells.size }}; }})()""")


def test_force_layout_fills_the_area(page):
    m = _metrics(page)
    assert m["w"] > 0.98 and m["h"] > 0.98, m
    assert m["cells"] >= GRID * GRID * 0.8, m


def test_spread_is_kept_after_window_resize(page):
    page.set_viewport_size({"width": 1200, "height": 800})
    page.wait_for_timeout(1500)
    m = _metrics(page)
    assert m["w"] > 0.95 and m["h"] > 0.95, m
    assert page.errors == []
