"""Lecture Quiz Maker — アプリの入口。

起動:  .venv/bin/streamlit run app.py

アプリの中だけで、講義資料のアップロード → 語彙の導出と抽出 → 地図 → 範囲選択 → クイズ → 採点・解説
までを通せる。データはコースごとに data/courses/<id>/ に置く（quizmap/courses.py）。
"""
import pandas  # noqa: F401  plotly などの遅延 import が Streamlit のスレッドと競合して落ちるのを防ぐ（§9 の 8）

import zlib
from pathlib import Path

import streamlit as st

from quizmap.common import DATA_DIR
from quizmap.courses import COURSES_DIR, course_info, current_job, delete_course, import_legacy, list_courses, running_job
from ui.i18n import LANGS, t
from ui.ingest_view import render_ingest_page, render_new_course
from ui.map_view import render_map_page
from ui.state import clear_course_state, save_course_name

NEW = "__new__"

# 画面の余白を詰め、左の地図を広く取る。
# 上の余白は Streamlit の上部のバー（stHeader、高さ 60px。透明だが横幅いっぱいでクリックを受ける）より下に
# 本文が来るように 3.6rem にする。これより詰めると、本文の上端のボタンや地図の上端が押せなくなる
PAGE_CSS = """
<style>
.block-container { padding: 3.6rem 1.6rem 1rem 1.6rem; max-width: 100%; }
header[data-testid="stHeader"] { background: transparent; }
div[data-testid="stVerticalBlockBorderWrapper"] { border-color: rgba(255,255,255,0.07); }
/* 地図の画面（ui/map_view.py）: 左の地図（qm-left）と右の欄（qm-right）の高さを窓の高さに合わせる（#43）。
   上の余白（約 74px）と下の余白（約 16px）を引き、420〜900px に収める。外側の枠が flex で高さを固定するので、そこも上書きする */
[data-testid="stLayoutWrapper"]:has(> .st-key-qm-left), [data-testid="stLayoutWrapper"]:has(> .st-key-qm-right),
.st-key-qm-left, .st-key-qm-right {
  height: clamp(420px, calc(100vh - 92px), 900px) !important; flex: 0 0 auto !important;
}
.st-key-qm-left { overflow: hidden !important; }
</style>
"""


@st.cache_resource
def _import_legacy_once() -> bool:
    """以前の置き場（data/ 直下）に取り込み済みのデータがあれば、コースとしてコピーする。1 プロセス 1 回。"""
    import_legacy(DATA_DIR)
    return True


def _select_course(cid: str) -> None:
    """選んでいるコースを cid にする。前と違うときだけ、前のコースの選択・クイズの状態を捨てる。

    選んでいるコースはウィジェットの状態ではなく session_state["course_id"] で持つ（#52, #16）。
    コース欄は表示名（言語・「未取り込み」・コース名）が変わるたびに作り直すため、その値には頼らない。
    """
    if st.session_state.get("course_id") != cid:
        clear_course_state()
    st.session_state["course_id"] = cid
    if cid == NEW:
        st.session_state.pop("course_dir", None)
    else:
        st.session_state["course_dir"] = str(COURSES_DIR / cid)


def _on_course_change(widget_key: str) -> None:
    cid = st.session_state.get(widget_key)
    if cid is not None and cid != st.session_state.get("course_id"):
        _select_course(cid)


def _delete_course_control(course: dict) -> None:
    """コースの削除。元に戻せないので、押したあとに確認を挟む。"""
    key = f"confirm_delete-{course['id']}"
    if not st.session_state.get(key):
        if st.button(t("delete_course"), icon=":material/delete:", use_container_width=True, key=f"delete-{course['id']}"):
            st.session_state[key] = True
            st.rerun()
        return
    st.warning(t("delete_course_confirm", name=course["name"]))
    yes, no = st.columns(2)
    if yes.button(t("delete_course_yes"), type="primary", use_container_width=True, key=f"delete-yes-{course['id']}"):
        delete_course(course["dir"])
        clear_course_state()
        for k in (key, "course_id", "course_dir"):
            st.session_state.pop(k, None)
        st.rerun()
    if no.button(t("delete_course_no"), use_container_width=True, key=f"delete-no-{course['id']}"):
        st.session_state[key] = False
        st.rerun()


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

    # 新しく作ったコースへの切り替えは、選択欄を作る前に行う（#13）
    pending = st.session_state.pop("pending_course", None)
    if pending:
        _select_course(pending)

    courses = list_courses()
    by_id = {c["id"]: c for c in courses}
    cur = st.session_state.get("course_id")
    if cur is None or (cur not in by_id and cur != NEW):
        ready = [c for c in courses if c["ready"]]
        _select_course((ready or courses or [{"id": NEW}])[0]["id"])
    sel = st.session_state["course_id"]

    options = [c["id"] for c in courses] + [NEW]
    labels = {cid: t("new_course") if cid == NEW
              else by_id[cid]["name"] + ("" if by_id[cid]["ready"] else t("not_ingested")) for cid in options}
    # 表示名が変わったら別のウィジェットとして作り直す（閉じた欄の文字を今の表示名にする。#16）
    widget_key = f"course_select-{zlib.crc32(chr(31).join(labels[c] for c in options).encode()):08x}"

    with st.sidebar:
        st.markdown(f"### {t('app_title')}")
        st.segmented_control(t("language"), options=list(LANGS), format_func=LANGS.get, key="lang",
                             default="ja", label_visibility="collapsed")
        st.selectbox(
            t("course"), options=options, index=options.index(sel), key=widget_key,
            format_func=labels.get, on_change=_on_course_change, args=(widget_key,),
        )
        _job_indicator()

    if sel == NEW or sel not in by_id:
        render_new_course()
        return

    course = by_id[sel]
    st.session_state["course_dir"] = str(course["dir"])
    job = current_job(course["dir"])
    ingesting = bool(job and job.status == "running")
    ingest_key = f"show_ingest-{sel}"
    with st.sidebar:
        if course["ready"]:
            name = st.text_input(t("rename_course"), value=course["name"], key=f"rename-{sel}", help=t("rename_help"))
            if name.strip() and name.strip() != course["name"]:
                save_course_name(name.strip())
                st.rerun()
            # 資料の取り込みは「地図とクイズ」と並ぶ画面ではなく、資料を足す・取り込み直すときに開く別の操作
            st.divider()
            if not st.session_state.get(ingest_key) and not ingesting:
                if st.button(t("open_ingest"), icon=":material/upload_file:", use_container_width=True):
                    st.session_state[ingest_key] = True
                    st.rerun()
        if not ingesting:
            _delete_course_control(course)

    if not course["ready"] or ingesting or st.session_state.get(ingest_key):
        if course["ready"] and not ingesting:
            if st.button(t("back_to_map"), icon=":material/arrow_back:"):
                st.session_state[ingest_key] = False
                st.rerun()
        render_ingest_page(course["dir"])
    else:
        render_map_page()

main()
