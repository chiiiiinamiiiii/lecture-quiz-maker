"""§6 地図と範囲選択の画面。"""
import pandas  # noqa: F401  plotly の遅延 import が Streamlit のスレッドと競合して落ちるのを防ぐ（§9 の 8）

import json

import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

from ui.browser_js import HOME_JS, PAN_JS, SELECT_JS
from ui.layout import compute_weights, layout_relation, layout_timeline
from ui.quiz_view import quiz_section
from ui.state import course_dir, load_graph, save_course_name
from ui.theme import *  # noqa: F403  色と設定の定数

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
            name=kind, showlegend=False,
        ))
        # 凡例の見本は別トレースにして色を固定する（データ点の色を拾うと出題済み色に変わってしまう）
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers", name=kind, legend="legend", legendrank=rank,
                                 marker=dict(size=12, symbol=symbol_of[kind], color=COLOR_UNASKED,
                                             line=dict(width=1, color="white")), hoverinfo="skip"))

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


def render_map_page():
    """地図・範囲選択・確定。確定後はクイズの画面（quiz_view）に続く。"""
    graph, vocab, asked = load_graph()
    concepts = {c["id"]: c for c in graph["concepts"]}
    weights = compute_weights(graph)
    n_total = len(concepts)
    lectures = sorted({lec for c in graph["concepts"] for lec in c["lectures"]})
    st.session_state.setdefault("map_epoch", 0)

    course = graph.get("course") or vocab.get("course", "")
    new_course = st.text_input("コース名", value=course, key=f"course_name-{course_dir().name}",
                               help="ここで直した名前は、このコースのデータに保存される")
    if new_course.strip() and new_course != course:
        save_course_name(new_course.strip())
        st.rerun()

    with st.sidebar:
        st.header("地図の表示")
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


