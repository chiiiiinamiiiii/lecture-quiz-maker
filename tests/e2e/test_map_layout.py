"""地図の画面の配置の戻り確認（#43）: 小さい窓でも、地図・ツールバー・凡例・講義回ボタンが画面内に収まり、重ならない。"""
import pytest

from conftest import app, lecture_button, n_selected, node_xy, view

SIZES = [(1100, 560), (1440, 800)]      # ノート PC（サイドバーを開いた状態）と、やや大きい画面


def _rects(page) -> dict:
    return app(page, """(() => { const q = (s) => { const r = a.root.querySelector(s).getBoundingClientRect();
        return {l: r.left, r: r.right, t: r.top, b: r.bottom, w: r.width, h: r.height}; };
      const segs = [...a.root.querySelectorAll('.qm-seg button')].map(b => b.getBoundingClientRect().height);
      return {map: q('.qm-map'), tb: q('.qm-toolbar'), lec: q('.qm-lectures'), legend: q('.qm-legend'),
              hint: q('.qm-hint'), count: q('.qm-count'), segs, ih: innerHeight}; })()""")


def _overlap(a, b) -> bool:
    return a["l"] < b["r"] and b["l"] < a["r"] and a["t"] < b["b"] and b["t"] < a["b"]


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_map_fits_window_without_overlap(page, size):
    page.set_viewport_size({"width": size[0], "height": size[1]})
    lecture_button(page, "3")                      # 選択数の表示も出した状態で見る
    page.wait_for_timeout(1500)
    r = _rects(page)
    m = r["map"]
    assert m["b"] <= r["ih"], "地図の下端が窓の外に出ている"
    assert r["lec"]["b"] <= m["b"] and r["lec"]["b"] <= r["ih"], "講義回ボタンの列が見えない"
    assert r["tb"]["r"] <= m["r"], "ツールバーが地図の右端を越えている"
    assert all(h <= 30 for h in r["segs"]), "「つながり／講義回」が折り返している"
    assert _overlap(r["count"], r["tb"]) and r["count"]["r"] <= r["tb"]["r"], "選択数がツールバーの中に無い"
    assert not _overlap(r["hint"], r["legend"]), "説明文と凡例が重なっている"
    assert m["l"] <= r["legend"]["l"] and r["legend"]["r"] <= m["r"], "凡例が地図からはみ出している"
    assert app(page, "a.root.querySelector('.qm-legend').scrollWidth <= a.root.querySelector('.qm-legend').clientWidth + 1")
    right = page.locator(".st-key-qm-right").bounding_box()
    assert right["y"] + right["height"] <= r["ih"] + 1, "右の欄の下端が窓の外に出ている"


def test_map_height_follows_window(page):
    page.set_viewport_size({"width": 1440, "height": 800})
    page.wait_for_timeout(1500)
    h800 = _rects(page)["map"]["h"]
    page.set_viewport_size({"width": 1440, "height": 600})
    page.wait_for_timeout(1500)
    h600 = _rects(page)["map"]["h"]
    assert 190 <= h800 - h600 <= 210


def test_wheel_on_lecture_row_scrolls_row_not_map(page):
    page.set_viewport_size({"width": 1100, "height": 560})
    page.wait_for_timeout(1500)
    lec = page.locator(".qm-lectures").bounding_box()
    assert app(page, "a.root.querySelector('.qm-lectures').dataset.moreRight") == "1", "続きを示す印が無い"
    s0 = view(page)["scale"]
    page.mouse.move(lec["x"] + lec["width"] / 2, lec["y"] + lec["height"] / 2)
    page.mouse.wheel(0, 200)
    page.wait_for_timeout(500)
    assert app(page, "a.root.querySelector('.qm-lectures').scrollLeft") > 0
    assert view(page)["scale"] == s0


def test_selection_list_shows_two_rows(page):
    """選択が 2 個のとき、右の一覧の 2 行目が切れない。"""
    x0, y0, _ = node_xy(page, 0)
    x1, y1, _ = node_xy(page, 1)
    page.mouse.click(x0, y0)
    page.wait_for_timeout(800)
    page.keyboard.down("Shift")
    page.mouse.click(x1, y1)
    page.keyboard.up("Shift")
    page.wait_for_timeout(2500)
    assert n_selected(page) == 2
    clipped = page.evaluate("""() => { let e = document.querySelector('.st-key-qm-right [data-testid=stCheckbox]');
        while (e && !/(auto|scroll)/.test(getComputedStyle(e).overflowY)) e = e.parentElement;
        return e && !e.classList.contains('st-key-qm-right') ? [e.scrollHeight - e.clientHeight] : []; }""")
    assert clipped and all(c <= 1 for c in clipped), clipped


# ------------------------------------------------------------------ #75: 全体表示で端の点がツールバーや下の帯に隠れない

def _fit_violations(page) -> list:
    """全体表示の後、ツールバー（と列の見出し）の下端より上、または下の帯の上端より下にはみ出す点と名前。"""
    return app(page, """(() => { const m = a.el.getBoundingClientRect();
        const rel = (s) => { const r = a.root.querySelector(s).getBoundingClientRect(); return {t: r.top - m.top, b: r.bottom - m.top}; };
        let top = rel('.qm-toolbar').b;
        if (a.layout === 'timeline' && a.el.dataset.axis) {
          const ax = JSON.parse(a.el.dataset.axis).labels; if (ax.length) top = Math.max(top, ...ax.map(l => l.y + l.h)); }
        const bottom = Math.min(rel('.qm-hint').t, rel('.qm-legend').t, rel('.qm-lectures').t);
        const bad = [];
        for (const id of a.visibleIds()) { const n = a.byId.get(id); const [x, y] = a.toScreen(a.pos.get(id)); const r = a._radius(n);
          if (y - r < top) bad.push([n.label, 'top', Math.round(y - r), Math.round(top)]);
          if (y + r + 19 > bottom) bad.push([n.label, 'bottom', Math.round(y + r + 19), Math.round(bottom)]);
          if (x < 0 || x > a.w) bad.push([n.label, 'x', Math.round(x)]); }
        return bad; })()""")


@pytest.mark.parametrize("layout", ["force", "timeline"])
@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_fit_keeps_nodes_clear_of_bars(page, size, layout):
    page.set_viewport_size({"width": size[0], "height": size[1]})
    page.wait_for_timeout(1000)
    if layout == "timeline":
        page.locator(".qm-toolbar button[data-layout='timeline']").click()
    page.locator(".qm-toolbar button[data-act='fit']").click()
    page.wait_for_timeout(1200)                     # 全体表示のアニメーション（280ms）と描画を待つ
    assert _fit_violations(page) == []


def test_initial_view_keeps_nodes_clear_of_bars(page):
    """開いた直後（全体表示のボタンを押す前）も同じ。"""
    assert _fit_violations(page) == []


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_content_is_not_under_streamlit_header(page, size):
    """Streamlit の上部のバー（透明だが横幅いっぱいでクリックを受ける）の下に、地図や右の欄が入らない。
    上の余白を詰めすぎると、本文の上端のボタンや地図の上端が押せなくなる（取り込み画面の「地図とクイズに戻る」で起きた）。"""
    page.set_viewport_size({"width": size[0], "height": size[1]})
    page.wait_for_timeout(1500)
    header = page.locator("header[data-testid='stHeader']").bounding_box()
    hb = header["y"] + header["height"]
    m = _rects(page)["map"]
    right = page.locator(".st-key-qm-right").bounding_box()
    assert m["t"] >= hb and right["y"] >= hb
    x, y = m["l"] + 40, m["t"] + 3                     # 地図の上端のすぐ内側が、上部のバーでなく地図に当たる
    assert page.evaluate(f"() => !document.elementFromPoint({x}, {y}).closest('header')")
