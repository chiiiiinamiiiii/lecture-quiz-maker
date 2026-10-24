"""外部資料（Wikipedia）で補った関係（origin: "external"）の見せ方（#78 の画面の分担）。

- 地図では点線で描く。origin の無い関係（RAG より前の graph）と origin: "slide" は今までどおり実線
- 辺のツールチップに「外部資料（Wikipedia）で補った関係」と出典の記事名（evidence_url から）を出す
- 凡例に「外部資料で補った関係」を足す（その地図に外部資料の関係があるときだけ）

取り込み済みのコースの graph.json をコピーし、関係の一部に origin を書き足した偽の graph で確かめる。
API は呼ばない。
"""
import json
from urllib.parse import quote

import pytest

from conftest import app, copy_fixture_course, open_page, start_app

# 記事名に HTML になる文字を混ぜ、ツールチップで無害化されることも見る
TITLE = "Test <b>article</b> 外部"
URL = "https://ja.wikipedia.org/wiki/" + quote(TITLE.replace(" ", "_"))


def _fake_graph(graph: dict) -> dict:
    """4 本に 1 本を external、その次を明示的に slide にし、残りは origin を書かない。"""
    g = json.loads(json.dumps(graph))
    for i, r in enumerate(g["relations"]):
        r.pop("origin", None)
        r.pop("evidence_url", None)
        if i % 4 == 0:
            r["origin"] = "external"
            r["evidence_url"] = URL
        elif i % 4 == 1:
            r["origin"] = "slide"
    return g


# ------------------------------------------------------------------ Python 側: 何を送るか

def test_origin_and_article_title():
    from ui.map_component import article_title, is_external
    assert is_external({"origin": "external"}) is True
    assert is_external({"origin": "slide"}) is False
    assert is_external({}) is False                                     # origin の無い既存の関係は slide 扱い
    assert article_title({"evidence_url": URL}) == TITLE
    assert article_title({"evidence_url": "https://en.wikipedia.org/wiki/Machine_learning"}) == "Machine learning"
    assert article_title({"evidence_url": "https://example.com/x"}) == "https://example.com/x"   # 読めなければ URL
    assert article_title({}) == ""


def test_external_flag_is_sent_to_browser(graph, monkeypatch):
    import ui.map_component as mc
    sent = {}

    def fake_component(**kw):
        sent.update(kw["data"])
        return type("R", (), {"selection": None})()

    monkeypatch.setattr(mc, "_component", fake_component)
    mc.concept_map(_fake_graph(graph), set(), [], 0, "c")
    edges = sent["edges"]
    assert [e["external"] for e in edges] == [i % 4 == 0 for i in range(len(edges))]
    assert all(e["article"] == (TITLE if e["external"] else "") for e in edges)
    for k in ("external_rel", "external_legend", "external_src"):
        assert sent["texts"][k]


# ------------------------------------------------------------------ ブラウザ側: 偽の graph でアプリを起動

@pytest.fixture(scope="module")
def ext_url(tmp_path_factory):
    d = tmp_path_factory.mktemp("quizmap-external")
    course = copy_fixture_course(d)
    g = json.loads((course / "graph.json").read_text(encoding="utf-8"))
    (course / "graph.json").write_text(json.dumps(_fake_graph(g), ensure_ascii=False), encoding="utf-8")
    yield from start_app(d)


@pytest.fixture
def ext_page(browser, ext_url):
    pg = open_page(browser, ext_url)
    yield pg
    pg.context.close()


def _visible_edges(pg, external: bool) -> list[str]:
    return app(pg, f"""(() => {{ const v = a.visibleIds();
        return a.edges.filter((e) => !!e.external === {str(external).lower()} && v.has(e.s) && v.has(e.t)).map((e) => e.id); }})()""")


def test_external_edges_are_dashed(ext_page):
    pg = ext_page
    ext, solid = _visible_edges(pg, True), _visible_edges(pg, False)
    assert ext and solid, "表示中の辺に external と slide の両方が要る"
    # 描くたびに、辺の線ごとの点線の有無を記録する（点の輪や選択の図形は描いていない状態で数える）
    dashed = app(pg, """(() => { const ctx = a.ctx, orig = ctx.stroke.bind(ctx); let n = 0, all = 0;
        ctx.stroke = (...args) => { all += 1; if (ctx.getLineDash().length) n += 1; return orig(...args); };
        a._draw(); ctx.stroke = orig; return [n, all]; })()""")
    assert dashed[0] == len(ext), f"点線の線 {dashed[0]} 本、表示中の external の辺 {len(ext)} 本"
    assert dashed[1] >= len(ext) + len(solid)
    assert app(pg, "a.ctx.getLineDash().length") == 0, "描き終わったら点線を戻す"
    assert pg.errors == []


def _hover_edge(pg, external: bool) -> str:
    """表示中の external（または slide）の辺の中点にマウスを乗せ、ツールチップの (HTML, 文字) を返す。"""
    pos = app(pg, f"""(() => {{ const v = a.visibleIds(), r = a.canvas.getBoundingClientRect();
        for (const e of a.edges) {{
          if (!!e.external !== {str(external).lower()} || !v.has(e.s) || !v.has(e.t)) continue;
          const p = a.toScreen(a.pos.get(e.s)), q = a.toScreen(a.pos.get(e.t));
          const mx = (p[0] + q[0]) / 2, my = (p[1] + q[1]) / 2;
          if (mx < 60 || my < 80 || mx > a.w - 320 || my > a.h - 160) continue;   // ツールバーや帯の下は避ける
          const h = a.hit(mx, my);
          if (h && h.type === "edge" && h.id === e.id) return [mx + r.left, my + r.top, e.id];
        }}
        return null; }})()""")
    assert pos, "マウスを乗せられる辺が無い"
    x, y, eid = pos
    pg.mouse.move(x - 30, y - 30)
    pg.mouse.move(x, y, steps=4)
    pg.wait_for_timeout(400)
    assert app(pg, "a.hover && a.hover.id") == eid
    return app(pg, "a.tooltip.innerHTML"), app(pg, "a.tooltip.innerText")


def test_external_edge_tooltip_shows_source_article(ext_page):
    html, text = _hover_edge(ext_page, True)
    assert "外部資料（Wikipedia）で補った関係" in text
    assert f"出典: {TITLE}" in text
    assert "<b>article</b>" not in html, "記事名は HTML として無害化する"


def test_slide_edge_tooltip_has_no_external_note(ext_page):
    html, text = _hover_edge(ext_page, False)
    assert "外部資料" not in text
    assert "第" in text and "p." in text                                 # 今までどおりの出典（講義回とページ）


def test_legend_lists_external_relations(ext_page):
    legend = app(ext_page, "a.root.querySelector('.qm-legend').innerText")
    assert "外部資料で補った関係" in legend
    assert app(ext_page, "!!a.root.querySelector('.qm-legend i.dash')")


def test_legend_has_no_external_item_without_external_edges(page):
    """origin の無い（RAG より前の）graph では凡例に出さない。"""
    assert app(page, "a.edges.some((e) => e.external)") is False
    assert "外部資料" not in app(page, "a.root.querySelector('.qm-legend').innerText")


def test_external_texts_in_english():
    from ui.i18n import TEXTS
    for k in ("external_rel", "external_legend", "external_src"):
        assert TEXTS[k]["en"] and TEXTS[k]["en"] != TEXTS[k]["ja"]
