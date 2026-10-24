"""Lecture Quiz Maker — アプリの入口。

起動:  .venv/bin/streamlit run app.py

アプリの中だけで、講義資料のアップロード → 語彙の導出と抽出 → 地図 → 範囲選択 → クイズ → 採点・解説
までを通せる。データはコースごとに data/courses/<id>/ に置く（quizmap/courses.py）。
"""
import pandas  # noqa: F401  plotly の遅延 import が Streamlit のスレッドと競合して落ちるのを防ぐ（§9 の 8）

from pathlib import Path

import streamlit as st

from quizmap.common import DATA_DIR
from quizmap.courses import COURSES_DIR, import_legacy, list_courses, running_job
from ui.ingest_view import render_ingest_page, render_new_course
from ui.map_view import render_map_page
from ui.state import clear_course_state

NEW = "__new__"
PAGE_MAP = "地図とクイズ"
PAGE_INGEST = "資料の取り込み"


@st.cache_resource
def _import_legacy_once() -> bool:
    """以前の置き場（data/ 直下）に取り込み済みのデータがあれば、コースとしてコピーする。1 プロセス 1 回。"""
    import_legacy(DATA_DIR)
    return True


def _on_course_change() -> None:
    clear_course_state()
    sel = st.session_state["course_select"]
    if sel == NEW:
        st.session_state.pop("course_dir", None)
    else:
        st.session_state["course_dir"] = str(COURSES_DIR / sel)


def main() -> None:
    st.set_page_config(page_title="QUIZ MAP", layout="wide")
    _import_legacy_once()
    st.title("QUIZ MAP")

    courses = list_courses()
    by_id = {c["id"]: c for c in courses}
    if "course_select" not in st.session_state:
        ready = [c for c in courses if c["ready"]]
        first = (ready or courses or [{"id": NEW}])[0]["id"]
        st.session_state["course_select"] = first
        _on_course_change()

    with st.sidebar:
        st.header("コース")
        st.selectbox(
            "コース", options=[c["id"] for c in courses] + [NEW], key="course_select",
            format_func=lambda cid: "＋ 新しいコース" if cid == NEW
            else by_id[cid]["name"] + ("" if by_id[cid]["ready"] else "（未取り込み）"),
            on_change=_on_course_change, label_visibility="collapsed",
        )
        job = running_job()
        if job:
            st.caption(f"取り込み中: {Path(job.course_dir).name}（{round(job.progress * 100)}%）")

    sel = st.session_state["course_select"]
    if sel == NEW or sel not in by_id:
        render_new_course()
        return

    course = by_id[sel]
    st.session_state["course_dir"] = str(course["dir"])
    with st.sidebar:
        page = st.radio("画面", [PAGE_MAP, PAGE_INGEST], key="page", horizontal=True,
                        disabled=not course["ready"])
    if not course["ready"] or page == PAGE_INGEST:
        render_ingest_page(course["dir"])
    else:
        render_map_page()


main()
