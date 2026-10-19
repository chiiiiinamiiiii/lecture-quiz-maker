"""Lecture Quiz Maker — 概念マップ（仕様 §6）。

起動:  .venv/bin/streamlit run app.py
入力:  data/graph.json（extract.py の出力）, data/vocab.json, data/results.json（あれば）
"""
import pandas  # noqa: F401  plotly の遅延 import が Streamlit のスレッドと競合して落ちるのを防ぐ（§9 の 8）

import json
from pathlib import Path

import networkx as nx
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

from quizmap.common import DEFAULT_MODEL, make_client
from quizmap.quiz import generate_quiz

DATA_DIR = Path(__file__).resolve().parent / "data"
GRAPH_PATH = DATA_DIR / "graph.json"
OTHER_LABEL = "その他"
ALL_LECTURES = "all"
MIDPOINT_TRACE = "関係（辺の中点）"

# ---- 配色。背景・枠・辺はピンク系。点の 2 状態は「出題済み / 未出題」という情報なので、
#      ピンク背景の上で明度と彩度の両方がはっきり違う 2 色にする（濃淡だけの区別はしない）
PINK = "#D6336C"
BG = "#FFF5F8"
PLOT_BG = "#FFFBFC"                        # グラフ領域は周囲より少し明るくして範囲が分かるようにする
FRAME = "rgba(214,51,108,0.55)"            # グラフ領域の枠線
COLOR_UNASKED = "#2E1A47"   # 未出題: 暗い紫（低明度・低彩度）
COLOR_ASKED = "#00A3A3"     # 出題済み: 明るいティール（高明度・高彩度、ピンクの補色側）
COLOR_EDGE = "rgba(190,30,90,0.6)"
COLOR_EDGE_MID = "#C2185B"
COLOR_TEXT = "#3B1F2B"
SYMBOLS = ["circle", "square", "diamond", "triangle-up", "star", "hexagon", "cross",
           "pentagon", "triangle-down", "x"]

# ---- Plotly のツールバー: 矩形・投げ縄だけ残す。ズームは 2 本指、移動はスペース＋2 本指（PAN_JS）
PLOTLY_CONFIG = {
    "displayModeBar": True,
    "displaylogo": False,
    "scrollZoom": True,
    "doubleClick": "reset",
    "modeBarButtonsToRemove": [
        "zoom2d", "pan2d", "zoomIn2d", "zoomOut2d", "autoScale2d", "resetScale2d",
        "toImage", "hoverClosestCartesian", "hoverCompareCartesian", "toggleSpikelines",
    ],
}

# ---- ブラウザ側の補助スクリプト（components.html の iframe から親ドキュメントのグラフを操作する）。
#
# Streamlit は Plotly 図のスペックを要素 ID に含める（key_as_main_identity=False 固定）ため、
# 選択に応じて Python 側で図を変えると投げ縄の選択が消える。そこで図は選択に依存させず、
#  (1) 講義回ボタンの選択を投げ縄と同じ見た目（selectedpoints）にする
#  (2) 再実行で初期値に戻ってしまうツール（dragmode: pan / select / lasso）の選択を維持する
# を restyle / relayout で行う。
SELECT_JS = """
<script>
(function () {
  const pwin = window.parent, pdoc = pwin.document;
  const Q = __PAYLOAD__;
  function desiredPoints(gd, sel) {
    const idx = [], sp = [];
    gd.data.forEach((t, i) => {
      if (!Array.isArray(t.customdata)) return;
      const pts = [];
      t.customdata.forEach((cd, j) => {
        if (cd[1] === "node") { if (sel.has(cd[0])) pts.push(j); }
        else if (cd[1] === "edge") { const e = Q.edges[cd[0]]; if (e && sel.has(e[0]) && sel.has(e[1])) pts.push(j); }
      });
      idx.push(i); sp.push(pts);
    });
    return { idx, sp };
  }
  function same(a, b) {
    if (a == null && b == null) return true;
    if (a == null || b == null) return false;
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
    return true;
  }
  function applySelection(gd) {
    const d = gd.__qmDesired;
    if (!d || gd.__qmBusy) return;
    // 投げ縄の形（layout.selections）が残っていると Plotly が描画のたびに選択を再計算して
    // こちらの設定を上書きするので、Python 側が結果を受け取った後は形を消す（無限ループ防止）
    if ((gd.layout.selections || []).length) {
      pwin.Plotly.relayout(gd, { selections: [] }).then(() => applySelection(gd));
      return;
    }
    if (d.sel.size === 0) {
      if (gd.data.some(t => Array.isArray(t.customdata) && t.selectedpoints != null)) {
        pwin.Plotly.restyle(gd, { selectedpoints: null });
      }
      return;
    }
    const { idx, sp } = desiredPoints(gd, d.sel);
    const changed = idx.some((i, k) => !same(gd.data[i].selectedpoints, sp[k]));
    if (changed) pwin.Plotly.restyle(gd, { selectedpoints: sp }, idx);
  }
  function keepDragmode(gd) {
    if (pwin.__qmDragmode && gd._fullLayout.dragmode !== pwin.__qmDragmode) {
      pwin.Plotly.relayout(gd, { dragmode: pwin.__qmDragmode });
    }
  }
  function apply() {
    const gd = pdoc.querySelector(".js-plotly-plot");
    if (!gd || !gd.data || !gd._fullLayout || !pwin.Plotly) return;
    if (!gd.__qmInstalled) {
      gd.__qmInstalled = true;
      // ツール選択（pan / select / lasso）の変更を記録する（モードバーのクリックは relayout として届く）
      gd.on("plotly_relayout", ev => { if (ev && ev.dragmode && !pwin.__qmTempPan) pwin.__qmDragmode = ev.dragmode; });
      // ユーザーの操作中は触らない。新しく囲んだら、再実行で新しい状態が届くまで古い選択を押し付けない
      pdoc.addEventListener("mousedown", e => { if (e.target.closest && e.target.closest(".js-plotly-plot")) gd.__qmBusy = true; }, true);
      pdoc.addEventListener("mouseup", () => { setTimeout(() => { gd.__qmBusy = false; }, 300); }, true);
      gd.on("plotly_selecting", () => { gd.__qmDesired = null; });
      gd.on("plotly_selected", () => { gd.__qmDesired = null; });
      // Streamlit が図を描き直したときに選択状態を戻す。
      // dragmode はここで触らない（モードバー操作の最中に古い値で上書きしてしまう）
      gd.on("plotly_afterplot", () => applySelection(gd));
    }
    // 再実行で初期値（lasso）に戻った dragmode を、ユーザーが選んだツールに戻す
    keepDragmode(gd);
    if (gd.__qmLastNonce !== Q.nonce) {          // 選択の反映はこの再実行ぶんにつき 1 回
      gd.__qmLastNonce = Q.nonce;
      gd.__qmDesired = { sel: new Set(Q.selected) };
      applySelection(gd);
    }
  }
  [0, 250, 700, 1500, 3000].forEach(ms => setTimeout(apply, ms));
})();
</script>
"""

# 手のひらツール: スペースを押している間は、2 本指の移動＝平行移動、ドラッグ＝平行移動。
# スペースなしの 2 本指はズーム（scrollZoom）のまま。
PAN_JS = """
<script>
(function () {
  const pwin = window.parent, pdoc = pwin.document;
  if (pwin.__quizmapPanInstalled) return;
  pwin.__quizmapPanInstalled = true;
  const style = pdoc.createElement("style");
  style.textContent = ".quizmap-pan, .quizmap-pan .nsewdrag, .quizmap-pan .drag { cursor: grab !important; }";
  pdoc.head.appendChild(style);
  let space = false, savedDragmode = null, acc = { dx: 0, dy: 0 }, raf = null;
  const gdOf = () => pdoc.querySelector(".js-plotly-plot");
  const typing = () => {
    const a = pdoc.activeElement;
    return a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.isContentEditable);
  };
  function setPan(on) {
    space = on;
    const gd = gdOf();
    pdoc.querySelectorAll(".js-plotly-plot").forEach(g => g.classList.toggle("quizmap-pan", on));
    if (!gd || !gd._fullLayout || !pwin.Plotly) return;
    // スペース中はドラッグも平行移動にする。SELECT_JS がこれを「ユーザーの選択」と記録しないよう旗を立てる
    pwin.__qmTempPan = true;
    if (on) { savedDragmode = gd._fullLayout.dragmode; pwin.Plotly.relayout(gd, { dragmode: "pan" }); }
    else if (savedDragmode) { pwin.Plotly.relayout(gd, { dragmode: savedDragmode }); savedDragmode = null; }
    pwin.__qmTempPan = false;
  }
  pdoc.addEventListener("keydown", e => {
    if (e.code !== "Space" || typing()) return;
    e.preventDefault();                                   // ページのスクロールを止める
    if (space) return;
    // ボタンやピルにフォーカスが残っていると、スペースを離したときにそれが「押され」てしまう
    const a = pdoc.activeElement;
    if (a && a !== pdoc.body && a.blur) a.blur();
    setPan(true);
  }, true);
  pdoc.addEventListener("keyup", e => {
    if (e.code !== "Space") return;
    if (space) e.preventDefault();                        // フォーカス中のボタンを押させない
    if (space) setPan(false);
  }, true);
  pwin.addEventListener("blur", () => { if (space) setPan(false); });
  function flush() {
    raf = null;
    const gd = gdOf();
    if (!gd || !gd._fullLayout || !pwin.Plotly) return;
    const xa = gd._fullLayout.xaxis, ya = gd._fullLayout.yaxis;
    const dx = acc.dx * (xa.range[1] - xa.range[0]) / xa._length;
    const dy = acc.dy * (ya.range[1] - ya.range[0]) / ya._length;
    acc = { dx: 0, dy: 0 };
    pwin.Plotly.relayout(gd, {
      "xaxis.range": [xa.range[0] + dx, xa.range[1] + dx],
      "yaxis.range": [ya.range[0] - dy, ya.range[1] - dy],
    });
  }
  pdoc.addEventListener("wheel", e => {
    if (!space) return;                                   // スペースなし → Plotly のズームに任せる
    const gd = e.target.closest && e.target.closest(".js-plotly-plot");
    if (!gd) return;
    e.preventDefault();
    e.stopPropagation();
    acc.dx += e.deltaX; acc.dy += e.deltaY;               // 1 フレームにまとめてから描き直す
    if (!raf) raf = pwin.requestAnimationFrame(flush);
  }, { capture: true, passive: false });
})();
</script>
"""

# 表示を初期位置に戻すボタン（iframe の中から親のグラフを relayout する）
HOME_JS = """
<style>
  body { margin: 0; background: transparent; font-family: sans-serif; }
  button { width: 100%; height: 38px; border: 1px solid rgba(214,51,108,0.35); border-radius: 8px;
           background: #FFF5F8; color: #3B1F2B; font-size: 14px; cursor: pointer; }
  button:hover { border-color: #D6336C; color: #D6336C; }
</style>
<button onclick="(function(){ const w=window.parent, gd=w.document.querySelector('.js-plotly-plot');
  if(gd && w.Plotly) w.Plotly.relayout(gd, {'xaxis.range': __X__, 'yaxis.range': __Y__}); })()">表示を元に戻す</button>
"""


# ------------------------------------------------------------------ data

@st.cache_data
def load_pages():
    return json.loads((DATA_DIR / "pages.json").read_text(encoding="utf-8"))


@st.cache_data
def load_graph():
    graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    vocab = json.loads((DATA_DIR / "vocab.json").read_text(encoding="utf-8"))
    results_path = DATA_DIR / "results.json"
    asked = set()
    if results_path.exists():
        asked = set(json.loads(results_path.read_text(encoding="utf-8")).get("asked_concepts", []))
    return graph, vocab, asked


def save_course_name(name: str) -> None:
    """コース名を graph.json に書き戻す（§16.3 の暫定対応）。"""
    graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    graph["course"] = name
    GRAPH_PATH.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    load_graph.clear()


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


# ------------------------------------------------------------------ labels

def place_labels(ids, pos, labels, weights, width_px=1000, height_px=640):
    """ラベルの衝突判定。重みの高い順に置き、重なるなら上→下→右→左の順に逃がし、
    どこにも置けなければ非表示（None）にする。座標はおおよそのピクセル換算。"""
    if not ids:
        return {}
    xs = [pos[i][0] for i in ids]
    ys = [pos[i][1] for i in ids]
    sx = width_px * 0.9 / ((max(xs) - min(xs)) or 1)
    sy = height_px * 0.9 / ((max(ys) - min(ys)) or 1)
    px = {i: (pos[i][0] * sx, -pos[i][1] * sy) for i in ids}
    placed = [(x - 7, y - 7, x + 7, y + 7) for x, y in px.values()]  # マーカー本体

    def overlaps(a, b):
        return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])

    result = {}
    for cid in sorted(ids, key=lambda i: -weights[i]):
        x, y = px[cid]
        w, h = len(labels[cid]) * 10 + 4, 13
        options = {
            "top center": (x - w / 2, y - 9 - h, x + w / 2, y - 9),
            "bottom center": (x - w / 2, y + 9, x + w / 2, y + 9 + h),
            "middle right": (x + 9, y - h / 2, x + 9 + w, y + h / 2),
            "middle left": (x - 9 - w, y - h / 2, x - 9, y + h / 2),
        }
        result[cid] = None
        for name, box in options.items():
            if not any(overlaps(box, b) for b in placed):
                placed.append(box)
                result[cid] = name
                break
    return result


# ------------------------------------------------------------------ figure

def lecture_list(c) -> str:
    return "・".join(f"第{n}回" for n in c["lectures"])


def view_ranges(pos, visible_ids, timeline, lectures):
    """初期表示の範囲。時系列は x を講義回で固定、y は表示中の点に余白を足す。"""
    ys = [pos[i][1] for i in visible_ids] or [0]
    pad_y = (max(ys) - min(ys)) * 0.08 + 0.5
    y = [min(ys) - pad_y, max(ys) + pad_y]
    if timeline:
        return [min(lectures) - 0.5, max(lectures) + 0.5], y
    xs = [pos[i][0] for i in visible_ids] or [0]
    pad_x = (max(xs) - min(xs)) * 0.08 + 0.02
    return [min(xs) - pad_x, max(xs) + pad_x], y


def build_figure(graph, vocab, asked, pos, visible_ids: set[str], timeline: bool, lectures: list[int], weights):
    """図は選択状態に依存させない（依存させると Streamlit が選択をリセットする。SELECT_JS 参照）。"""
    concepts = {c["id"]: c for c in graph["concepts"]}
    kinds = [k["name"] for k in vocab["kinds"]] + [OTHER_LABEL]
    symbol_of = {k: SYMBOLS[i % len(SYMBOLS)] for i, k in enumerate(kinds)}
    fig = go.Figure()

    # --- 辺（表示中の概念どうしだけ）と、その中点マーカー
    ex, ey, mx, my, mtext, mcd = [], [], [], [], [], []
    for eid, r in enumerate(graph["relations"]):
        if r["from"] not in visible_ids or r["to"] not in visible_ids:
            continue
        (x0, y0), (x1, y1) = pos[r["from"]], pos[r["to"]]
        ex += [x0, x1, None]
        ey += [y0, y1, None]
        mx.append((x0 + x1) / 2)
        my.append((y0 + y1) / 2)
        mtext.append(f"{concepts[r['from']]['label']} —{r['label']}→ {concepts[r['to']]['label']}"
                     f"<br>第{r['source']['lecture']}回 p.{r['source']['page']}")
        mcd.append([f"e{eid}", "edge"])
    fig.add_trace(go.Scatter(x=ex, y=ey, mode="lines", line=dict(color=COLOR_EDGE, width=1.3),
                             hoverinfo="skip", showlegend=False, name="辺"))
    fig.add_trace(go.Scatter(x=mx, y=my, mode="markers",
                             marker=dict(size=5, color=COLOR_EDGE_MID, symbol="circle"),
                             customdata=mcd, hovertemplate="%{text}<extra></extra>", text=mtext,
                             name=MIDPOINT_TRACE, legend="legend", legendrank=1000))

    # --- ノード: kind ごとに 1 トレース（凡例 1 段目 = 種類（形））。色は 出題済み / 未出題 の 2 色
    labels = {i: concepts[i]["label"] for i in visible_ids}
    textpos = place_labels(sorted(visible_ids), pos, labels, weights)
    for rank, kind in enumerate(kinds):
        ids = sorted(cid for cid in visible_ids if concepts[cid]["kind"] == kind)
        if not ids:
            continue
        fig.add_trace(go.Scatter(
            x=[pos[i][0] for i in ids], y=[pos[i][1] for i in ids],
            mode="markers+text",
            text=[labels[i] if textpos.get(i) else "" for i in ids],
            textposition=[textpos.get(i) or "top center" for i in ids],
            textfont=dict(size=10, color=COLOR_TEXT),
            marker=dict(size=12, symbol=symbol_of[kind],
                        color=[COLOR_ASKED if i in asked else COLOR_UNASKED for i in ids],
                        line=dict(width=1, color="white")),
            customdata=[[i, "node"] for i in ids],
            hovertext=[f"<b>{concepts[i]['label']}</b>（{kind}）<br>{concepts[i]['definition']}"
                       f"<br>{lecture_list(concepts[i])}" for i in ids],
            hovertemplate="%{hovertext}<extra></extra>",
            name=kind, legend="legend", legendrank=rank,
        ))

    # --- 凡例 2 段目 = 状態（色）
    for name, color in (("未出題", COLOR_UNASKED), ("出題済み", COLOR_ASKED)):
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers", name=name, legend="legend2",
                                 marker=dict(size=12, color=color, symbol="circle"), hoverinfo="skip"))

    x_range, y_range = view_ranges(pos, visible_ids, timeline, lectures)
    frame = dict(showline=True, mirror=True, linecolor=FRAME, linewidth=1.5, showgrid=False, zeroline=False)
    if timeline:
        xaxis = dict(frame, range=x_range, tick0=1, dtick=1,
                     tickmode="array", tickvals=lectures, ticktext=[f"第{n}回" for n in lectures],
                     title="講義回", color=COLOR_TEXT)
    else:
        xaxis = dict(frame, range=x_range, showticklabels=False)
    legend_common = dict(orientation="h", yanchor="bottom", x=0, font=dict(color=COLOR_TEXT, size=12),
                         title=dict(font=dict(color=COLOR_TEXT, size=12)))
    fig.update_layout(
        dragmode="lasso", height=760, margin=dict(l=10, r=10, t=90, b=10),
        legend=dict(legend_common, y=1.085, title_text="種類（形）"),
        legend2=dict(legend_common, y=1.01, title_text="状態（色）"),
        xaxis=xaxis,
        yaxis=dict(frame, range=y_range, showticklabels=False),
        uirevision="map",   # 再実行で図を描き直しても、ズーム・移動・選んだツールを保つ
        plot_bgcolor=PLOT_BG, paper_bgcolor=BG,
        newselection=dict(line=dict(color=PINK, width=2)),
        activeselection=dict(fillcolor=PINK, opacity=0.12),
        modebar=dict(bgcolor="rgba(0,0,0,0)", color="#B05A7A", activecolor=PINK),
    )
    fig.update_traces(selected=dict(marker=dict(opacity=1.0)),
                      unselected=dict(marker=dict(opacity=0.3)))
    return fig


# ------------------------------------------------------------------ selection

def expand_selection(graph, picked_nodes: set[str], picked_edges: set[str]):
    """候補 = 囲んだ概念 ＋ 囲んだ辺の中点から拾った両端（§6 選択 1, 3）。1 次隣接は加えない。"""
    reason: dict[str, str] = {cid: "囲んだ" for cid in picked_nodes}
    for eid in picked_edges:
        r = graph["relations"][int(eid[1:])]
        for cid in (r["from"], r["to"]):
            reason.setdefault(cid, "辺の中点")
    return reason


def reset_selection() -> None:
    """確定済み・選択中の状態を空にし、地図の選択も解除する（チャートの key を変えて作り直す）。"""
    st.session_state.pop("confirmed_ids", None)
    st.session_state.pop("lasso", None)
    st.session_state["map_epoch"] = st.session_state.get("map_epoch", 0) + 1
    st.session_state["lecture_pills"] = []
    for k in [k for k in st.session_state if k.startswith("chk-")]:
        del st.session_state[k]


def select_component(selected: set[str], graph, nonce: int) -> None:
    edges = {f"e{eid}": [r["from"], r["to"]] for eid, r in enumerate(graph["relations"])}
    payload = json.dumps({"selected": sorted(selected), "edges": edges, "nonce": nonce}, ensure_ascii=False)
    components.html(SELECT_JS.replace("__PAYLOAD__", payload), height=0)


# ------------------------------------------------------------------ main

def main():
    st.set_page_config(page_title="QUIZ MAP", layout="wide")
    graph, vocab, asked = load_graph()
    concepts = {c["id"]: c for c in graph["concepts"]}
    weights = compute_weights(graph)
    n_total = len(concepts)
    lectures = sorted({lec for c in graph["concepts"] for lec in c["lectures"]})
    st.session_state.setdefault("map_epoch", 0)

    st.title("QUIZ MAP")
    course = graph.get("course") or vocab.get("course", "")
    new_course = st.text_input("コース名", value=course, key="course_name",
                               help="フォルダ名が入ってしまっている場合はここで直す（§16.3）。data/graph.json に保存される")
    if new_course.strip() and new_course != course:
        save_course_name(new_course.strip())
        st.rerun()

    with st.sidebar:
        st.header("表示")
        layout = st.radio("レイアウト", ["時系列（講義回ごと）", "関係ベース（力学的）"], index=0)
        n_show = st.slider("表示する概念の数", min_value=1, max_value=n_total, value=n_total,
                           help="データは常に全部保持し、表示だけを絞る")
        st.caption(f"概念 {n_total} / 関係 {len(graph['relations'])} / 出題済み {len(asked)}")
        st.caption("地図右上の投げ縄（Lasso）か矩形（Box）で囲む。"
                   "2本指でズーム、スペースを押しながら2本指（またはドラッグ）で移動。"
                   "ダブルクリックか「表示を元に戻す」で初期表示")

    # 講義回ボタン: 押した回の概念を「投げ縄で囲んだ」のと同じ扱いで選択する（複数回は和集合）。「全部」で全回
    picked = st.pills("講義回で選ぶ", options=[ALL_LECTURES] + lectures,
                      format_func=lambda n: "全部" if n == ALL_LECTURES else f"第{n}回",
                      selection_mode="multi", key="lecture_pills") or []
    picked_lectures = set(lectures) if ALL_LECTURES in picked else set(picked)
    lecture_ids = {cid for cid, c in concepts.items() if picked_lectures & set(c["lectures"])}

    # 表示する概念: 重みの降順で上位 n_show 個
    order = sorted(concepts, key=lambda i: -weights[i])
    visible = set(order[:n_show])
    timeline = layout.startswith("時系列")
    pos = layout_timeline(graph) if timeline else layout_relation(graph)

    # リセットと「表示を元に戻す」は投げ縄ツールの近く（地図の右上）
    _, bar_m, bar_r = st.columns([5, 1, 1])
    x_home, y_home = view_ranges(pos, visible, timeline, lectures)
    with bar_m:
        components.html(HOME_JS.replace("__X__", json.dumps(x_home)).replace("__Y__", json.dumps(y_home)), height=40)
    bar_r.button("選択をリセット", on_click=reset_selection, use_container_width=True)

    chart_key = f"map-{'t' if timeline else 'r'}-{n_show}-{st.session_state['map_epoch']}"
    fig = build_figure(graph, vocab, asked, pos, visible, timeline, lectures, weights)
    event = st.plotly_chart(fig, on_select="rerun", selection_mode=("lasso", "box"),
                            config=PLOTLY_CONFIG, key=chart_key)
    components.html(PAN_JS, height=0)

    ev_nodes, ev_edges = set(), set()
    for p in event.selection.points:
        cd = p.get("customdata")
        if not cd:
            continue
        ident, typ = cd[0], cd[1]
        (ev_nodes if typ == "node" else ev_edges).add(ident)
    # 投げ縄の結果は保持する（表示を揃えるためにブラウザ側で投げ縄の形を消すと、以後のイベントが空になる）。
    # 新しく囲めば置き換わり、「選択をリセット」で消える
    if ev_nodes or ev_edges:
        st.session_state["lasso"] = (sorted(ev_nodes), sorted(ev_edges))
    lasso_nodes, lasso_edges = st.session_state.get("lasso", ([], []))
    picked_nodes = set(lasso_nodes) | lecture_ids
    picked_edges = set(lasso_edges)

    candidates = expand_selection(graph, picked_nodes, picked_edges)
    # 地図上の見た目を投げ縄と揃える（講義回ボタン分も含めた選択を selectedpoints で反映）
    st.session_state["sel_nonce"] = st.session_state.get("sel_nonce", 0) + 1
    select_component(set(candidates) & visible, graph, st.session_state["sel_nonce"])

    st.subheader("選択")
    if not candidates:
        st.info("地図上で概念を囲むか、講義回ボタンを押すと、ここに候補が出ます。")
        if "confirmed_ids" in st.session_state:
            st.write("確定済みの概念 id:", st.session_state["confirmed_ids"])
        return

    rank = {"囲んだ": 0, "辺の中点": 1}
    ordered = sorted(candidates, key=lambda i: (rank[candidates[i]], -weights[i]))
    n_mid = sum(1 for v in candidates.values() if v == "辺の中点")
    st.caption(f"囲んだ {len(picked_nodes)} 個 ＋ 辺の中点から {n_mid} 個 = 候補 {len(candidates)} 個。"
               "不要なものはチェックを外してください。")

    with st.form("confirm"):
        cols = st.columns(3)
        checked = {}
        for i, cid in enumerate(ordered):
            c = concepts[cid]
            label = f"{c['label']}（{c['kind']}）"
            if candidates[cid] != "囲んだ":
                label += f" ← {candidates[cid]}"
            if cid in asked:
                label += " ✓出題済み"
            checked[cid] = cols[i % 3].checkbox(label, value=True, key=f"chk-{cid}")
        submitted = st.form_submit_button("この選択で確定", type="primary")

    if submitted:
        st.session_state["confirmed_ids"] = [cid for cid in ordered if checked[cid]]

    if "confirmed_ids" in st.session_state:
        quiz_section(graph, vocab, st.session_state["confirmed_ids"])


def quiz_section(graph, vocab, ids: list[str]) -> None:
    """§7 クイズ生成。確定した概念 id と問題数から、その場で生成する。採点（§8）は未実装。"""
    st.subheader("クイズ生成")
    col_n, col_b = st.columns([1, 2])
    n_q = col_n.number_input("問題数", min_value=1, max_value=20, value=5, step=1)
    col_b.caption(f"確定した {len(ids)} 概念から {n_q} 問作る")
    if col_b.button("クイズを生成", type="primary"):
        with st.spinner("生成中（生成 → 誤答の自己確認 の 2 回呼び出し）…"):
            try:
                result = generate_quiz(make_client(), DEFAULT_MODEL, graph, vocab, load_pages(), ids, int(n_q))
            except Exception as e:  # noqa: BLE001  画面に理由を出して止める
                st.error(f"生成に失敗: {e}")
                return
        st.session_state["quiz"] = result
        (DATA_DIR / "quiz_latest.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = st.session_state.get("quiz")
    if not result:
        return
    m = result["meta"]
    st.caption(f"{m['n_generated']} 問 / 内訳 {m['aspect_actual']} / 形式 {m['type_actual']} / "
               f"誤答差し替え {m['distractors_replaced']} 件 / API {m['api_calls']} 回 / {m['seconds']} 秒")
    for w in result["warnings"]:
        st.warning(w)
    st.json(result["questions"], expanded=True)


main()
