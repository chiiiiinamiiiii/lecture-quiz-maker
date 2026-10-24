"""表示順の重みと、2 種類の配置（時系列 / 関係ベース）。"""
import pandas  # noqa: F401  plotly の遅延 import が Streamlit のスレッドと競合して落ちるのを防ぐ（§9 の 8）

import networkx as nx
import streamlit as st

@st.cache_data
def compute_weights(graph) -> dict[str, float]:
    """表示順の重み: 出現回数とつながりの本数を正規化して合成（§6）。"""
    occ = {c["id"]: len(c["sources"]) for c in graph["concepts"]}
    deg = {c["id"]: 0 for c in graph["concepts"]}
    for r in graph["relations"]:
        deg[r["from"]] += 1
        deg[r["to"]] += 1

    def norm(d):
        hi = max(d.values()) or 1
        return {k: v / hi for k, v in d.items()}

    o, g = norm(occ), norm(deg)
    return {cid: 0.5 * o[cid] + 0.5 * g[cid] for cid in occ}


@st.cache_data
def layout_relation(graph) -> dict[str, tuple[float, float]]:
    """関係ベース: 力学的レイアウト。連結成分ごとに配置し、最大成分を主領域、残りを下段に並べる。"""
    G = nx.Graph()
    G.add_nodes_from(c["id"] for c in graph["concepts"])
    G.add_edges_from((r["from"], r["to"]) for r in graph["relations"])
    comps = sorted(nx.connected_components(G), key=len, reverse=True)

    def spring_unit(nodes) -> dict:
        sub = G.subgraph(nodes)
        if len(sub) == 1:
            return {next(iter(sub)): (0.5, 0.5)}
        pos = nx.spring_layout(sub, seed=42, k=1.8 / max(1, len(sub) ** 0.5), iterations=500)
        xs = [x for x, _ in pos.values()]
        ys = [y for _, y in pos.values()]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        return {k: ((x - x0) / ((x1 - x0) or 1), (y - y0) / ((y1 - y0) or 1)) for k, (x, y) in pos.items()}

    pos: dict[str, tuple[float, float]] = {}
    if not comps:
        return pos
    pos.update(spring_unit(comps[0]))
    rest = comps[1:]
    total = sum(len(c) for c in rest) or 1
    x, row = 0.0, 0
    for comp in rest:
        w = max(0.06, len(comp) / total)
        if x + w > 1.0 and x > 0:
            x, row = 0.0, row + 1
        y_top = -0.12 - row * 0.22
        for k, (px, py) in spring_unit(comp).items():
            pos[k] = (x + 0.02 + px * (w - 0.04), y_top - 0.02 - py * 0.14)
        x += w
    return {k: (float(px), float(py)) for k, (px, py) in pos.items()}


@st.cache_data
def layout_timeline(graph) -> dict[str, tuple[float, float]]:
    """時系列: 初出の講義回を x、同じ回の中では重みの降順で y に並べる。"""
    weights = compute_weights(graph)
    by_lecture: dict[int, list[str]] = {}
    for c in graph["concepts"]:
        by_lecture.setdefault(min(c["lectures"]), []).append(c["id"])
    pos = {}
    for lec, ids in by_lecture.items():
        ids.sort(key=lambda i: -weights[i])
        n = len(ids)
        for row, cid in enumerate(ids):
            pos[cid] = (lec + (0.15 if row % 2 else -0.15), -(row - (n - 1) / 2))
    return pos


