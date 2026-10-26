"""同じスライドに出るだけの関係（origin: "cooccurrence"、固定ラベル「関連」）の見せ方（#127）。

- 外部資料の点線（[4, 4]）とも区別できる、薄く細かい点線
- ツールチップに「同じスライドに出る（第◯回 p.◯）」
- 凡例に「同じスライドに出る（関連）」（その地図にあるときだけ）
- ラベル「関連」は画面の言語で出す（English は Related）。「その他」と同じ扱い

取り込み済みのコースの graph.json の関係に origin を書き足した偽の graph で確かめる。API は呼ばない。
"""
import json

import pytest

from conftest import app, copy_fixture_course, open_page, start_app

URL = "https://ja.wikipedia.org/wiki/Test"


def _fake_graph(graph: dict) -> dict:
    """3 本に 1 本を cooccurrence（ラベル「関連」、evidence なし）、その次を external、残りは origin を書かない。"""
    g = json.loads(json.dumps(graph))
    for i, r in enumerate(g["relations"]):
        for k in ("origin", "evidence_url"):
            r.pop(k, None)
        if i % 3 == 0:
            r["origin"] = "cooccurrence"
            r["label"] = "関連"
            r["evidence"] = ""
        elif i % 3 == 1:
            r["origin"] = "external"
            r["evidence_url"] = URL
    return g


# ------------------------------------------------------------------ Python 側

def test_origin_of():
    from ui.map_component import origin_of
    assert origin_of({}) == "slide"
    assert origin_of({"origin": "cooccurrence"}) == "cooccurrence"
    assert origin_of({"origin": "external"}) == "external"


def test_related_label_follows_screen_language():
    import streamlit as st
    from ui.map_component import RELATED_LABEL, rel_label
    assert RELATED_LABEL == "関連"
    st.session_state["lang"] = "en"
    try:
        assert rel_label("関連") == "Related"
        assert rel_label("その他") == "Other"
        assert rel_label("提唱") == "提唱"
    finally:
        st.session_state["lang"] = "ja"
    assert rel_label("関連") == "関連"


def test_cooccur_flag_is_sent(graph, monkeypatch):
    import ui.map_component as mc
    sent = {}
    monkeypatch.setattr(mc, "_component", lambda **kw: (sent.update(kw["data"]), type("R", (), {"selection": None})())[1])
    mc.concept_map(_fake_graph(graph), set(), [], 0, "c")
    edges = sent["edges"]
    assert [e["cooccur"] for e in edges] == [i % 3 == 0 for i in range(len(edges))]
    assert [e["external"] for e in edges] == [i % 3 == 1 for i in range(len(edges))]
    assert all(e["label"] == "関連" for e in edges if e["cooccur"])
    for k in ("cooccur_rel", "cooccur_legend"):
        assert sent["texts"][k]


# ------------------------------------------------------------------ ブラウザ側

@pytest.fixture(scope="module")
def co_url(tmp_path_factory):
    d = tmp_path_factory.mktemp("quizmap-cooccur")
    course = copy_fixture_course(d)
    g = json.loads((course / "graph.json").read_text(encoding="utf-8"))
    (course / "graph.json").write_text(json.dumps(_fake_graph(g), ensure_ascii=False), encoding="utf-8")
    yield from start_app(d)


@pytest.fixture
def co_page(browser, co_url):
    pg = open_page(browser, co_url)
    yield pg
    pg.context.close()


def test_three_line_styles(co_page):
    """関係の線は 3 種類: 実線（slide）、[4, 4]（external）、[1.5, 3] で薄い（cooccurrence）。"""
    pg = co_page
    res = app(pg, """(() => { const ctx = a.ctx, orig = ctx.stroke.bind(ctx), seen = {};
        ctx.stroke = (...args) => { const k = JSON.stringify(ctx.getLineDash()) + "|" + ctx.lineWidth;
          seen[k] = (seen[k] || 0) + 1; return orig(...args); };
        a._draw(); ctx.stroke = orig;
        const v = a.visibleIds(), vis = a.edges.filter((e) => v.has(e.s) && v.has(e.t));
        return {seen, co: vis.filter((e) => e.cooccur).length, ext: vis.filter((e) => e.external).length}; })()""")
    seen, co, ext = res["seen"], res["co"], res["ext"]
    assert co and ext
    dashed = {k: n for k, n in seen.items() if not k.startswith("[]")}
    assert sum(n for k, n in dashed.items() if k.startswith("[1.5,3]")) == co
    assert sum(n for k, n in dashed.items() if k.startswith("[4,4]")) == ext
    # cooccurrence の線は、ほかの辺より細い
    co_w = {float(k.split("|")[1]) for k in dashed if k.startswith("[1.5,3]")}
    ext_w = {float(k.split("|")[1]) for k in dashed if k.startswith("[4,4]")}
    assert max(co_w) < min(ext_w)
    assert pg.errors == []


def _hover_cooccur(pg):
    pos = app(pg, """(() => { const v = a.visibleIds(), r = a.canvas.getBoundingClientRect();
        for (const e of a.edges) {
          if (!e.cooccur || !v.has(e.s) || !v.has(e.t)) continue;
          const p = a.toScreen(a.pos.get(e.s)), q = a.toScreen(a.pos.get(e.t));
          const mx = (p[0] + q[0]) / 2, my = (p[1] + q[1]) / 2;
          if (mx < 60 || my < 80 || mx > a.w - 320 || my > a.h - 160) continue;
          const h = a.hit(mx, my);
          if (h && h.type === "edge" && h.id === e.id) return [mx + r.left, my + r.top, e.id, e.lecture, e.page];
        }
        return null; })()""")
    assert pos, "マウスを乗せられる cooccurrence の辺が無い"
    x, y, eid, lec, page = pos
    pg.mouse.move(x - 30, y - 30)
    pg.mouse.move(x, y, steps=4)
    pg.wait_for_timeout(400)
    assert app(pg, "a.hover && a.hover.id") == eid
    return app(pg, "a.tooltip.innerText"), lec, page


def test_cooccur_tooltip(co_page):
    text, lec, page = _hover_cooccur(co_page)
    assert f"同じスライドに出る（第{lec}回 p.{page}）" in text
    assert "関連" in text
    assert "外部資料" not in text


def test_cooccur_tooltip_in_english(co_page):
    pg = co_page
    pg.get_by_text("English", exact=True).click()
    pg.wait_for_function("() => window.__qmMapApp && window.__qmMapApp.T.cooccur_rel.startsWith('Appear')", timeout=30000)
    pg.wait_for_timeout(1000)
    text, lec, page = _hover_cooccur(pg)
    assert f"Appear on the same slide (L{lec} p.{page})" in text
    assert "Related" in text and "関連" not in text


def test_legend_lists_cooccur_and_external(co_page):
    legend = app(co_page, "a.root.querySelector('.qm-legend').innerText")
    assert "同じスライドに出る（関連）" in legend
    assert "外部資料で補った関係" in legend
    assert app(co_page, "!!a.root.querySelector('.qm-legend i.dots')")


def test_legend_without_cooccur(page):
    assert "同じスライドに出る" not in app(page, "a.root.querySelector('.qm-legend').innerText")


@pytest.mark.parametrize("size", [(1100, 700), (1600, 1000)])
def test_legend_items_are_not_clipped(browser, co_url, size):
    """凡例の項目が 5 つに増えても、どれも凡例の枠と地図の中に収まる（狭い窓では折り返す）。"""
    ctx = browser.new_context(viewport={"width": size[0], "height": size[1]})
    pg = ctx.new_page()
    pg.goto(co_url, wait_until="networkidle")
    pg.wait_for_function("() => window.__qmMapApp && window.__qmMapApp.view", timeout=60000)
    pg.wait_for_timeout(1500)
    res = app(pg, """(() => { const L = a.root.querySelector('.qm-legend').getBoundingClientRect();
        const C = a.canvas.getBoundingClientRect();
        const H = a.root.querySelector('.qm-hint').getBoundingClientRect();
        return {items: [...a.root.querySelectorAll('.qm-legend span')].map((s) => { const r = s.getBoundingClientRect();
                  return [s.innerText, r.left >= L.left - 0.5 && r.right <= L.right + 0.5 && r.right <= C.right + 0.5]; }),
                hint: H.width}; })()""")
    ctx.close()
    assert len(res["items"]) == 5
    assert all(ok for _, ok in res["items"]), res["items"]
    assert res["hint"] >= 100, "操作の説明が潰れていない"
