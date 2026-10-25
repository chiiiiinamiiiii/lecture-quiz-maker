"""資料の取り込み画面。PDF のアップロード → 語彙の導出と抽出（裏で実行）→ 進捗表示。

入れた PDF はすべて使う。講義回はファイル名から読み、読めないものには続きの番号を自動で振る（利用者の割り当ては無い）。
"""
from pathlib import Path

import streamlit as st

from quizmap.common import lecture_number_of
from quizmap.courses import (
    PHASE_LABEL, assign_lecture_numbers, course_info, create_course, current_job, rag_enabled, running_job,
    save_course_info, save_upload, start_ingest, start_rag_rebuild,
)
from ui.i18n import TEXTS, t
from ui.state import clear_course_state

SECONDS_PER_LECTURE = 15     # 目安。概念と関係で 2 回呼び、呼び出しの間に待機が入る


def _add_files(d: Path, uploaded) -> tuple[int, int]:
    """アップロードされた PDF を保存し、一覧に足す。(新しく足した数, 既に一覧にあった数) を返す。

    新しい名前はファイル名から講義回を推測する。既に一覧にある名前（save_upload が同じ中身と
    判断して同じ名前を返したもの）は、講義回をそのまま残す（#6）。最後に、読めない・重なる講義回に
    続きの番号を振り、すべてを「使う」にする。
    """
    info = course_info(d)
    known = {f["name"]: f for f in info["files"]}
    added = kept = 0
    for up in uploaded:
        path = save_upload(d, up.name, up.getvalue())
        if path.name in known:
            kept += 1
            continue
        n = lecture_number_of(path)
        known[path.name] = {"name": path.name, "lecture": n, "use": True}
        added += 1
    info["files"] = assign_lecture_numbers(list(known.values()))
    save_course_info(d, info)
    return added, kept


def needs_rag_index(d: Path) -> bool:
    """取り込み済みで、検索の索引（rag_index.json）がまだ無いコースか。QUIZMAP_RAG=off なら常に False。"""
    return rag_enabled() and (d / "graph.json").exists() and not (d / "rag_index.json").exists()


def render_new_course() -> None:
    st.subheader(t("new_course_title"))
    st.caption(t("new_course_help"))
    name = st.text_input(t("course_name"), placeholder=t("course_name_ph"))
    files = st.file_uploader(t("pdf_files"), type=["pdf"], accept_multiple_files=True,
                             key="new_course_files")
    if st.button(t("create_course"), type="primary", disabled=not (name.strip() and files)):
        d = create_course(name.strip())
        _add_files(d, files)
        clear_course_state()
        st.session_state["course_dir"] = str(d)
        st.session_state["pending_course"] = d.name
        st.rerun()


def render_ingest_page(d: Path) -> None:
    info = course_info(d)
    st.subheader(t("ingest_title", name=info["name"]))

    job = current_job(d)
    busy = running_job()
    if job and job.status == "running":
        _progress(d)
        return

    # 追加のアップロード
    more = st.file_uploader(t("add_files"), type=["pdf"], accept_multiple_files=True,
                            key=f"more-{d.name}-{st.session_state.get('upload_epoch', 0)}")
    if more and st.button(t("add_btn")):
        added, kept = _add_files(d, more)
        st.session_state["upload_epoch"] = st.session_state.get("upload_epoch", 0) + 1
        msg = []
        if added:
            msg.append(t("added_toast", n=added))
        if kept:
            msg.append(t("kept_toast", n=kept))
        st.session_state["ingest_notice"] = t("sentence_sep").join(msg)   # rerun の直前の toast は消えることがあるので次の回で出す
        st.rerun()
    notice = st.session_state.pop("ingest_notice", None)
    if notice:
        st.toast(notice)

    files = course_info(d)["files"]
    if not files:
        st.info(t("no_files"))
        return

    # 入れたものは全部使う。前の版で「使わない」にしていた資料や、番号の無い資料にも番号を振り直す
    assigned = assign_lecture_numbers(files)
    if assigned != files:
        info = course_info(d)
        info["files"] = assigned
        save_course_info(d, info)
        files = assigned
    used = files
    st.markdown(t("files_title", n=len(files)))
    with st.expander(t("files_list"), expanded=len(files) <= 6):
        st.markdown("\n".join(f"- {t('lecture_fmt', n=f['lecture'])}　{f['name']}" for f in files))

    has_vocab = (d / "vocab.json").exists()
    rebuild = False
    if has_vocab:
        rebuild = st.checkbox(t("rebuild_vocab"), value=False, help=t("rebuild_help"))

    minutes = max(1, round(len(used) * SECONDS_PER_LECTURE / 60))
    st.caption(t("ingest_estimate", n=len(used), m=minutes))
    if busy and busy.course_dir != str(d):
        st.warning(t("other_running"))
    label = t("reingest") if (d / "graph.json").exists() else t("start_ingest")
    if st.button(label, type="primary", disabled=bool(not used or busy)):
        try:
            start_ingest(d, rebuild_vocab=rebuild)
        except ValueError as e:
            st.error(str(e))
            return
        st.rerun()

    # RAG より前のコースや、索引づくりが途中で止まったコースに、あとから索引と関係の補完を作る
    if needs_rag_index(d):
        st.caption(t("rag_rebuild_help"))
        if st.button(t("rag_rebuild_btn"), disabled=bool(busy)):
            try:
                start_rag_rebuild(d)
            except ValueError as e:
                st.error(str(e))
                return
            st.rerun()

    if job and job.status == "error":
        st.error(t("last_failed", e=job.error))
        with st.expander(t("log")):
            st.code("\n".join(job.log[-40:]))
    elif job and job.status == "done":
        st.success(t("last_done", t=job.finished_at))


@st.fragment(run_every="2s")
def _progress(d: Path) -> None:
    job = current_job(d)
    if job is None:
        return
    if job.status != "running":
        st.rerun(scope="app")          # 終わったら画面全体を描き直す（地図を開けるようにする）
    st.progress(min(1.0, job.progress), text=f"{_phase_label(job.phase)}…")
    st.caption(t("progress_note", t=job.started_at))
    st.code("\n".join(job.log[-12:]) or t("starting"))


def _phase_label(phase: str) -> str:
    key = f"phase_{phase}"
    return t(key) if key in TEXTS else PHASE_LABEL.get(phase, phase)
