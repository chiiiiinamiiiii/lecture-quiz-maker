"""「つながり」の配置（ui/layout.py）: どの関係にもつながらない概念を、地図の下に格子状に並べない（#119 に関連）。

同じページ（無ければ同じ講義回）に出る概念のそばに置く。最大の連結成分の形は今までと同じ。
ブラウザは使わない（配置の計算だけを見る）。
"""
import math
import statistics

import networkx as nx
import pytest

from ui.layout import _cooccurrence_edges, layout_relation


def _concept(cid, pages, lecture=1):
    return {"id": cid, "label": cid, "kind": "k", "definition": "",
            "sources": [{"lecture": lecture, "page": p} for p in pages], "lectures": [lecture]}


def _rel(a, b):
    return {"from": a, "to": b, "label": "r", "source": {"lecture": 1, "page": 1}}


@pytest.fixture
def toy():
    """主成分 m0〜m9（鎖）、それと同じページに出るつながらない概念 i0〜i9、どこにも同じページの無い z（講義回 2）。"""
    concepts = [_concept(f"m{i}", [i]) for i in range(10)]
    concepts += [_concept(f"i{i}", [i]) for i in range(10)]
    concepts += [_concept("z", [99], lecture=2), _concept("z2", [98], lecture=2)]
    rels = [_rel(f"m{i}", f"m{i + 1}") for i in range(9)]
    return {"concepts": concepts, "relations": rels}


def _d(p, a, b):
    return math.hypot(p[a][0] - p[b][0], p[a][1] - p[b][1])


def test_all_concepts_placed_and_deterministic(toy):
    p1 = layout_relation(toy)
    assert set(p1) == {c["id"] for c in toy["concepts"]}
    assert all(math.isfinite(x) and math.isfinite(y) for x, y in p1.values())
    p2 = layout_relation.__wrapped__(toy) if hasattr(layout_relation, "__wrapped__") else layout_relation(toy)
    assert p1 == p2


def test_unconnected_concept_sits_near_its_page_mate(toy):
    """同じページの概念から、関係の辺の長さ（主成分の辺の長さの中央値）の 3 倍以内に置く。"""
    p = layout_relation(toy)
    L = statistics.median(_d(p, f"m{i}", f"m{i + 1}") for i in range(9))
    for i in range(10):
        assert _d(p, f"i{i}", f"m{i}") < 3 * L, f"i{i} が同じページの m{i} から遠い"
    far = max(_d(p, f"i{i}", f"m{j}") for i in range(10) for j in range(10))
    assert far > 3 * L                                     # 判定がゆるすぎないこと（遠い組はちゃんと遠い）


def test_concepts_without_page_mates_stay_on_the_map(toy):
    """同じページに相手がいない概念も、地図の外へ飛んでいかない。"""
    p = layout_relation(toy)
    xs = [p[f"m{i}"][0] for i in range(10)]
    ys = [p[f"m{i}"][1] for i in range(10)]
    span = max(max(xs) - min(xs), max(ys) - min(ys))
    for z in ("z", "z2"):
        assert min(xs) - 2 * span < p[z][0] < max(xs) + 2 * span
        assert min(ys) - 2 * span < p[z][1] < max(ys) + 2 * span


def test_no_grid_rows_of_unconnected_concepts(graph):
    """取り込み済みのコースで、つながらない概念が同じ高さの行に並ばない（前は 1 行に十数個ずつ並んでいた）。"""
    p = layout_relation(graph)
    G = nx.Graph()
    G.add_nodes_from(c["id"] for c in graph["concepts"])
    G.add_edges_from((r["from"], r["to"]) for r in graph["relations"])
    isolated = [n for n in G if G.degree(n) == 0]
    if len(isolated) < 5:
        pytest.skip("つながらない概念が少ないコース")
    ys = [round(p[n][1], 3) for n in isolated]
    most = max(ys.count(y) for y in set(ys))
    assert most <= 2, f"同じ高さに {most} 個並んでいる"


def test_main_component_shape_is_kept(graph):
    """最大の連結成分は、今までと同じく単独で計算した形（0〜1 に正規化）のまま。"""
    G = nx.Graph()
    G.add_nodes_from(c["id"] for c in graph["concepts"])
    G.add_edges_from((r["from"], r["to"]) for r in graph["relations"])
    main = max(nx.connected_components(G), key=len)
    if len(main) < 2:
        pytest.skip("関係が無いコース")
    raw = nx.spring_layout(G.subgraph(main), seed=42, k=1.8 / len(main) ** 0.5, iterations=500)
    xs = [x for x, _ in raw.values()]
    ys = [y for _, y in raw.values()]
    p = layout_relation(graph)
    for n, (x, y) in raw.items():
        ex = (x - min(xs)) / ((max(xs) - min(xs)) or 1)
        ey = (y - min(ys)) / ((max(ys) - min(ys)) or 1)
        assert p[n] == pytest.approx((ex, ey), abs=1e-9)


def test_invisible_edges_are_not_drawn(graph, monkeypatch):
    """見えない引力は配置だけに使い、地図に送る辺は関係の数のまま。"""
    import ui.map_component as mc
    sent = {}
    monkeypatch.setattr(mc, "_component", lambda **kw: (sent.update(kw["data"]), type("R", (), {"selection": None})())[1])
    mc.concept_map(graph, set(), [], 0, "c")
    assert len(sent["edges"]) == len(graph["relations"])
    assert _cooccurrence_edges(graph)                       # 配置には使っている


def test_no_grid_rows_in_toy(toy):
    """つながらない概念 12 個が同じ高さの行に並ばない（前の配置では 1 行に並んでいた）。"""
    p = layout_relation(toy)
    ys = [round(p[n][1], 3) for n in p if not n.startswith("m")]
    assert max(ys.count(y) for y in set(ys)) <= 2
    ms = [p[f"m{i}"][1] for i in range(10)]
    assert sum(min(ms) - 0.1 > y for y in ys) < len(ys) / 2, "半分以上が主成分の下に積まれている"
