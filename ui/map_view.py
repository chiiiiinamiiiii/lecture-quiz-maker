"""§6 地図と範囲選択の画面。左にほぼ全面の地図、右に選択した範囲・問題・解説。"""
import streamlit as st

from ui.i18n import t
from ui.layout import compute_weights
from ui.map_component import concept_map
from ui.quiz_view import quiz_section
from ui.state import course_dir, load_graph

MAP_HEIGHT = 780


def _current_selection() -> tuple[list[str], set[str]]:
    sel = st.session_state.get("selection") or {}
    return list(sel.get("ids", [])), set(sel.get("via_edge", []))


def render_map_page() -> None:
    graph, vocab, asked = load_graph()
    concepts = {c["id"]: c for c in graph["concepts"]}
    weights = compute_weights(graph)
    st.session_state.setdefault("sel_version", 0)
    version = st.session_state["sel_version"]

    left, right = st.columns([1.75, 1], gap="medium")
    with left:
        ids, _ = _current_selection()
        sel = concept_map(graph, asked, ids, version, course_dir().name, height=MAP_HEIGHT)
        # ブラウザ側の選択を受け取る。古い版（コース切替・解除の前）の送信は捨てる
        # 右側はこの後に描くので、ここで受け取れば同じ実行の中で一覧に反映される
        if sel is not None and sel.get("version") == version:
            st.session_state["selection"] = sel

    with right:
        panel = st.container(height=MAP_HEIGHT, border=False)
        with panel:
            _selection_panel(graph, concepts, weights, asked)
            if "confirmed_ids" in st.session_state:
                quiz_section(graph, vocab, st.session_state["confirmed_ids"])


def _selection_panel(graph, concepts, weights, asked) -> None:
    st.subheader(t("sel_title"))
    ids, via = _current_selection()
    ids = [i for i in ids if i in concepts]
    if not ids:
        st.caption(t("sel_empty"))
        return
    ordered = sorted(ids, key=lambda i: (i in via, -weights[i]))
    st.caption(t("sel_caption", n=len(ordered), m=sum(1 for i in ordered if i in via)))
    with st.form("confirm", border=False):
        box = st.container(height=min(260, 34 * len(ordered) + 10), border=True)
        checked = {}
        with box:
            for cid in ordered:
                c = concepts[cid]
                label = c["label"]
                if cid in via:
                    label += f"  ·  {t('via_edge')}"
                if cid in asked:
                    label += f"  ·  {t('asked_mark')}"
                checked[cid] = st.checkbox(label, value=True, key=f"chk-{cid}")
        if st.form_submit_button(t("confirm"), type="primary", use_container_width=True):
            st.session_state["confirmed_ids"] = [cid for cid in ordered if checked[cid]]
            for k in ("quiz", "graded", "hints", "results_saved", "grade_calls"):
                st.session_state.pop(k, None)
    if "confirmed_ids" in st.session_state:
        st.caption(t("confirmed", n=len(st.session_state["confirmed_ids"])))
