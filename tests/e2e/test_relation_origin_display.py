"""関係の出どころ（資料 / 外部資料 / 同じスライド）は、地図の見た目で区別しない。

線はすべて同じ実線で、凡例にも辺の説明にも出どころを書かない。
取り込み済みのコースの graph.json に origin を書き足した偽の graph で確かめる。API は呼ばない。
"""
import json

import pytest

from conftest import app, copy_fixture_course, open_page, start_app

ORIGINS = ("external", "cooccurrence", "slide")


@pytest.fixture(scope="module")
def mixed_url(tmp_path_factory):
    d = tmp_path_factory.mktemp("quizmap-origin")
    course = copy_fixture_course(d)
    g = json.loads((course / "graph.json").read_text(encoding="utf-8"))
    for i, r in enumerate(g["relations"]):
        r["origin"] = ORIGINS[i % 3]
        if r["origin"] == "external":
            r["evidence_url"] = "https://ja.wikipedia.org/wiki/Test"
    (course / "graph.json").write_text(json.dumps(g, ensure_ascii=False), encoding="utf-8")
    yield from start_app(d)


@pytest.fixture
def mixed_page(browser, mixed_url):
    pg = open_page(browser, mixed_url)
    yield pg
    pg.context.close()


def test_every_edge_is_drawn_solid_with_same_width(mixed_page):
    seen = app(mixed_page, """(() => { const ctx = a.ctx, orig = ctx.stroke.bind(ctx); let dashed = 0, n = 0;
        const widths = new Set();
        ctx.stroke = (...args) => { n += 1; widths.add(ctx.lineWidth); if (ctx.getLineDash().length) dashed += 1; return orig(...args); };
        a._draw(); ctx.stroke = orig; return [dashed, n, [...widths]]; })()""")
    assert seen[1] > 0
    assert seen[0] == 0, f"点線の線が {seen[0]} 本ある"
    assert len(seen[2]) == 1, f"線の太さが複数ある: {seen[2]}"


def test_legend_does_not_mention_origin(mixed_page):
    legend = app(mixed_page, "a.root.querySelector('.qm-legend').innerText")
    assert "外部資料" not in legend and "同じスライド" not in legend


def test_edge_tooltip_shows_only_relation_and_page(mixed_page):
    texts = app(mixed_page, """(() => a.edges.map((e) => { a.hover = {type: "edge", id: e.id}; a._showTooltip(10, 10);
        return a.tooltip.innerText; }))()""")
    assert texts
    assert not any("外部資料" in t or "同じスライド" in t or "出典" in t for t in texts)
    assert all("p." in t for t in texts)
