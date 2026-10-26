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


PAGE_PULL = 0.08      # 同じページに出る概念どうしの見えない引力（関係の辺は 1）
LECTURE_PULL = 0.02   # 同じページに相手がいない概念を、同じ講義回の概念に寄せる引力


def _cooccurrence_edges(graph) -> dict[tuple[str, str], float]:
    """配置の計算だけに使う見えない辺 {(a, b): 重み}。地図には描かない。

    同じページに出る概念の対に、ページの概念の数で割った弱い引力を足す（多くの概念が出るページで
    引力が強くなりすぎないように）。同じページに相手がいない概念は、同じ講義回の概念に、さらに弱く寄せる。
    """
    by_page: dict[tuple[int, int], list[str]] = {}
    by_lecture: dict[int, list[str]] = {}
    for c in graph["concepts"]:
        for s in c["sources"]:
            by_page.setdefault((s["lecture"], s["page"]), []).append(c["id"])
        for n in c["lectures"]:
            by_lecture.setdefault(n, []).append(c["id"])
    w: dict[tuple[str, str], float] = {}

    def add(a, b, x):
        if a != b:
            key = (a, b) if a < b else (b, a)
            w[key] = w.get(key, 0.0) + x

    for ids in by_page.values():
        ids = sorted(set(ids))
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                add(a, b, PAGE_PULL / (len(ids) - 1))
    paired = {x for k in w for x in k}
    for c in graph["concepts"]:
        if c["id"] in paired:
            continue
        mates = sorted({m for n in c["lectures"] for m in by_lecture[n]} - {c["id"]})
        for m in mates:
            add(c["id"], m, LECTURE_PULL / len(mates))
    return w


@st.cache_data
def layout_relation(graph) -> dict[str, tuple[float, float]]:
    """関係ベース: 力学的レイアウト。

    最大の連結成分を今までどおり先に配置して固定し、残り（どの関係にもつながらない概念と小さな成分）は、
    関係の辺に加えて「同じページ（無ければ同じ講義回）に出る概念どうしの弱い見えない引力」で、
    関係の近い概念のそばに置く（#119。前は地図の下に格子状に並べていて、水平の行ばかりになった）。
    """
    G = nx.Graph()
    G.add_nodes_from(c["id"] for c in graph["concepts"])
    G.add_edges_from((r["from"], r["to"]) for r in graph["relations"])
    comps = sorted(nx.connected_components(G), key=len, reverse=True)
    if not comps:
        return {}
    main = comps[0]
    k = 1.8 / max(1, len(main) ** 0.5)

    pos: dict[str, tuple[float, float]] = {}
    sub = G.subgraph(main)
    if len(sub) == 1:
        pos[next(iter(sub))] = (0.5, 0.5)
    else:
        raw = nx.spring_layout(sub, seed=42, k=k, iterations=500)
        xs = [x for x, _ in raw.values()]
        ys = [y for _, y in raw.values()]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        pos = {n: ((x - x0) / ((x1 - x0) or 1), (y - y0) / ((y1 - y0) or 1)) for n, (x, y) in raw.items()}
    if len(comps) == 1:
        return {n: (float(x), float(y)) for n, (x, y) in pos.items()}

    return _place_rest(graph, G, main, pos)


def _place_rest(graph, G: nx.Graph, main: set, fixed: dict) -> dict[str, tuple[float, float]]:
    """主成分（fixed）を動かさずに、残りの概念を置く。

    引く力: 関係の辺（重み 1）と、同じページ・同じ講義回の見えない辺（弱い重み）。自然長はそれぞれ
    主成分の辺の長さの中央値（L）とその 1.5 倍。押す力は近く（2L 以内）の概念どうしだけに働かせる
    （遠くまで押す普通の力学的配置では、弱い引力しか無い概念が地図の外へ飛んでいく）。
    """
    import numpy as np

    ids = [c["id"] for c in graph["concepts"]]
    idx = {n: i for i, n in enumerate(ids)}
    free = np.array([n not in main for n in ids])
    lens = [np.hypot(fixed[a][0] - fixed[b][0], fixed[a][1] - fixed[b][1]) for a, b in G.subgraph(main).edges]
    L = float(np.median(lens)) if lens else 0.1

    edges = [(idx[a], idx[b], 1.0, L) for a, b in G.edges]
    real = {frozenset(e) for e in G.edges}
    edges += [(idx[a], idx[b], x, 1.5 * L) for (a, b), x in _cooccurrence_edges(graph).items()
              if frozenset((a, b)) not in real]
    E = np.array([(i, j) for i, j, _, _ in edges], dtype=int).reshape(-1, 2)
    W = np.array([w for _, _, w, _ in edges])
    R = np.array([r for _, _, _, r in edges])

    rng = np.random.default_rng(42)
    P = np.zeros((len(ids), 2))
    for n, (x, y) in fixed.items():
        P[idx[n]] = (x, y)
    # はじめの位置: 主成分の中で引かれている相手の重み付き平均（相手がいなければ地図の中心）の近く
    center = P[~free].mean(axis=0)
    for i in np.flatnonzero(free):
        m = ((E[:, 0] == i) & ~free[E[:, 1]]) | ((E[:, 1] == i) & ~free[E[:, 0]])
        if m.any():
            other = np.where(E[m, 0] == i, E[m, 1], E[m, 0])
            base = (P[other] * W[m, None]).sum(axis=0) / W[m].sum()
        else:
            base = center
        P[i] = base + rng.normal(scale=L, size=2)

    deg_w = np.zeros(len(ids))
    np.add.at(deg_w, E[:, 0], W)
    np.add.at(deg_w, E[:, 1], W)
    deg_w = np.maximum(deg_w, 1e-9)
    near = 2 * L
    for it in range(300):
        step = 0.5 * (1 - it / 300) + 0.02
        D = P[E[:, 1]] - P[E[:, 0]]
        d = np.maximum(np.hypot(D[:, 0], D[:, 1]), 1e-9)
        f = (W * (d - R) / d)[:, None] * D                 # 自然長に戻す力
        F = np.zeros_like(P)
        np.add.at(F, E[:, 0], f)
        np.add.at(F, E[:, 1], -f)
        F /= deg_w[:, None]                                # 引かれる相手の数・重みによらず、同じ速さで寄る
        Q = P[free][:, None, :] - P[None, :, :]
        q = np.maximum(np.hypot(Q[..., 0], Q[..., 1]), 1e-9)
        push = np.where(q < near, (near - q) / near, 0.0)
        push[np.arange(push.shape[0]), np.flatnonzero(free)] = 0.0
        F[free] += 0.5 * L * (push[..., None] * Q / q[..., None]).sum(axis=1)   # 1 つの相手から最大 L/2
        P[free] += step * F[free]
    return {n: (float(P[i, 0]), float(P[i, 1])) for n, i in idx.items()}


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


