"""資料の取り込み画面。PDF のアップロード → 講義回の割り当て → 語彙の導出と抽出（裏で実行）→ 進捗表示。"""
from pathlib import Path

import pandas as pd
import streamlit as st

from quizmap.common import lecture_number_of
from quizmap.courses import (
    PHASE_LABEL, course_info, create_course, current_job, lecture_numbers_conflict, running_job,
    save_course_info, save_upload, start_ingest,
)
from ui.i18n import TEXTS, t
from ui.state import clear_course_state

SECONDS_PER_LECTURE = 15     # 目安。概念と関係で 2 回呼び、呼び出しの間に待機が入る


def _add_files(d: Path, uploaded) -> tuple[int, int]:
    """アップロードされた PDF を保存し、一覧に足す。(新しく足した数, 既に一覧にあった数) を返す。

    新しい名前はファイル名から講義回を推測する。既に一覧にある名前（save_upload が同じ中身と
    判断して同じ名前を返したもの）は、利用者が決めた講義回と「使う」をそのまま残す（#6）。
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
        known[path.name] = {"name": path.name, "lecture": n, "use": n is not None}
        added += 1
    info["files"] = sorted(known.values(), key=lambda f: (f["lecture"] is None, f["lecture"] or 0, f["name"]))
    save_course_info(d, info)
    return added, kept


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

    st.markdown(t("assign_title"))
    # 列名は言語によらない名前にし、見出しだけ訳す（言語を切り替えても表の編集状態が崩れないように）
    df = pd.DataFrame([{"use": f["use"], "lecture": f["lecture"], "file": f["name"]} for f in files])
    edited = st.data_editor(
        df, hide_index=True, use_container_width=True, key=f"files-{d.name}",
        column_config={
            "use": st.column_config.CheckboxColumn(t("col_use"), width="small"),
            "lecture": st.column_config.NumberColumn(t("col_lecture"), min_value=1, max_value=99, step=1,
                                                     width="small"),
            "file": st.column_config.TextColumn(t("col_file"), disabled=True),
        },
        disabled=["file"],
    )
    new_files = []
    for _, row in edited.iterrows():
        n = None if pd.isna(row["lecture"]) else int(row["lecture"])
        new_files.append({"name": row["file"], "lecture": n, "use": bool(row["use"]) and n is not None})
    if new_files != files:
        info = course_info(d)
        info["files"] = new_files
        save_course_info(d, info)

    used = [f for f in new_files if f["use"]]
    dup = lecture_numbers_conflict(new_files)
    if dup:
        st.error(t("dup_lectures", l=t("list_sep").join(t("lecture_fmt", n=n) for n in dup)))

    has_vocab = (d / "vocab.json").exists()
    rebuild = False
    if has_vocab:
        rebuild = st.checkbox(t("rebuild_vocab"), value=False, help=t("rebuild_help"))

    minutes = max(1, round(len(used) * SECONDS_PER_LECTURE / 60))
    st.caption(t("ingest_estimate", n=len(used), m=minutes))
    if busy and busy.course_dir != str(d):
        st.warning(t("other_running"))
    label = t("reingest") if (d / "graph.json").exists() else t("start_ingest")
    if st.button(label, type="primary", disabled=bool(dup or not used or busy)):
        try:
            start_ingest(d, rebuild_vocab=rebuild)
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
