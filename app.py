"""Lecture Quiz Maker — アプリの入口。

起動:  .venv/bin/streamlit run app.py

アプリの中だけで、講義資料のアップロード → 語彙の導出と抽出 → 地図 → 範囲選択 → クイズ → 採点・解説
までを通せる。データはコースごとに data/courses/<id>/ に置く（quizmap/courses.py）。
"""
import pandas  # noqa: F401  plotly などの遅延 import が Streamlit のスレッドと競合して落ちるのを防ぐ（§9 の 8）

from pathlib import Path

import streamlit as st

from quizmap.common import DATA_DIR
from quizmap.courses import COURSES_DIR, course_info, current_job, import_legacy, list_courses, running_job
from ui.i18n import LANGS, t
from ui.ingest_view import render_ingest_page, render_new_course
from ui.map_view import render_map_page
from ui.state import clear_course_state, load_graph, save_course_name

NEW = "__new__"

# 画面の余白を詰め、左の地図を広く取る
PAGE_CSS = """
<style>
.block-container { padding: 1.4rem 1.6rem 1rem 1.6rem; max-width: 100%; }
header[data-testid="stHeader"] { background: transparent; }
div[data-testid="stVerticalBlockBorderWrapper"] { border-color: rgba(255,255,255,0.07); }
</style>
"""


@st.cache_resource
def _import_legacy_once() -> bool:
    """以前の置き場（data/ 直下）に取り込み済みのデータがあれば、コースとしてコピーする。1 プロセス 1 回。"""
    import_legacy(DATA_DIR)
    return True


def _select_course(cid: str) -> None:
    clear_course_state()
    if cid == NEW:
        st.session_state.pop("course_dir", None)
    else:
        st.session_state["course_dir"] = str(COURSES_DIR / cid)


def _on_course_change() -> None:
    _select_course(st.session_state["course_select"])


@st.fragment(run_every="2s")
def _job_indicator() -> None:
    """取り込み中のコースと進み具合（どの画面にいても見える）。終わったら画面全体を描き直す（#16）。"""
    job = running_job()
    was = st.session_state.get("job_running")
    if job:
        name = course_info(Path(job.course_dir)).get("name", "")
        st.caption(f"{t('ingesting')}: {name}")
        st.progress(min(1.0, job.progress))
        st.session_state["job_running"] = job.course_dir
    elif was:
        st.session_state.pop("job_running", None)
        st.rerun(scope="app")


def main() -> None:
    st.set_page_config(page_title="QUIZ MAP", layout="wide", initial_sidebar_state="expanded")
    _import_legacy_once()
    st.markdown(PAGE_CSS, unsafe_allow_html=True)

    # 新しく作ったコースへの切り替えは、選択欄を作る前に行う（作った後に値を変えるとエラーになる: #13）
    pending = st.session_state.pop("pending_course", None)
    if pending:
        st.session_state["course_select"] = pending
        _select_course(pending)

    courses = list_courses()
    by_id = {c["id"]: c for c in courses}
    if "course_select" not in st.session_state or (st.session_state["course_select"] not in by_id
                                                    and st.session_state["course_select"] != NEW):
        ready = [c for c in courses if c["ready"]]
        first = (ready or courses or [{"id": NEW}])[0]["id"]
        st.session_state["course_select"] = first
        _select_course(first)

    with st.sidebar:
        st.markdown(f"### {t('app_title')}")
        st.segmented_control(t("language"), options=list(LANGS), format_func=LANGS.get, key="lang",
                             default="ja", label_visibility="collapsed")
        st.selectbox(
            t("course"), options=[c["id"] for c in courses] + [NEW], key="course_select",
            format_func=lambda cid: t("new_course") if cid == NEW
            else by_id[cid]["name"] + ("" if by_id[cid]["ready"] else t("not_ingested")),
            on_change=_on_course_change,
        )
        _job_indicator()

    sel = st.session_state["course_select"]
    if sel == NEW or sel not in by_id:
        render_new_course()
        return

    course = by_id[sel]
    st.session_state["course_dir"] = str(course["dir"])
    job = current_job(course["dir"])
    ingesting = bool(job and job.status == "running")
    with st.sidebar:
        options = ["map", "ingest"]
        page = st.segmented_control(t("page"), options=options, key=f"page-{sel}", default="map",
                                    format_func=lambda p: t("page_map") if p == "map" else t("page_ingest"),
                                    disabled=not course["ready"] or ingesting) or "map"
        if course["ready"]:
            graph, _, asked = load_graph()
            st.caption(t("stats", c=len(graph["concepts"]), r=len(graph["relations"]), a=len(asked)))
            name = st.text_input(t("rename_course"), value=course["name"], key=f"rename-{sel}", help=t("rename_help"))
            if name.strip() and name.strip() != course["name"]:
                save_course_name(name.strip())
                st.rerun()

    if not course["ready"] or ingesting or page == "ingest":
        render_ingest_page(course["dir"])
    else:
        render_map_page()


main()
