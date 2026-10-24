"""資料の取り込み画面。PDF のアップロード → 講義回の割り当て → 語彙の導出と抽出（裏で実行）→ 進捗表示。"""
from pathlib import Path

import pandas as pd
import streamlit as st

from quizmap.common import lecture_number_of
from quizmap.courses import (
    PHASE_LABEL, course_info, create_course, current_job, lecture_numbers_conflict, running_job,
    save_course_info, save_upload, start_ingest,
)
from ui.state import clear_course_state

SECONDS_PER_LECTURE = 15     # 目安。概念と関係で 2 回呼び、呼び出しの間に待機が入る


def _add_files(d: Path, uploaded) -> int:
    """アップロードされた PDF を保存し、ファイル名から講義回を推測して一覧に足す。"""
    info = course_info(d)
    known = {f["name"]: f for f in info["files"]}
    added = 0
    for up in uploaded:
        path = save_upload(d, up.name, up.getvalue())
        n = lecture_number_of(path)
        entry = {"name": path.name, "lecture": n, "use": n is not None}
        if path.name not in known:
            added += 1
        known[path.name] = entry
    info["files"] = sorted(known.values(), key=lambda f: (f["lecture"] is None, f["lecture"] or 0, f["name"]))
    save_course_info(d, info)
    return added


def render_new_course() -> None:
    st.subheader("新しいコースを作る")
    st.caption("講義資料の PDF をまとめてアップロードしてください。ファイル名の「第3回」「week3」"
               "「Lecture 03」などから講義回を読み取ります。読み取れないものは次の画面で指定できます。")
    name = st.text_input("コース名", placeholder="例: 倫理学の世界")
    files = st.file_uploader("講義資料（PDF、複数可）", type=["pdf"], accept_multiple_files=True,
                             key="new_course_files")
    if st.button("コースを作る", type="primary", disabled=not (name.strip() and files)):
        d = create_course(name.strip())
        _add_files(d, files)
        clear_course_state()
        st.session_state["course_dir"] = str(d)
        st.session_state["course_select"] = d.name
        st.rerun()


def render_ingest_page(d: Path) -> None:
    info = course_info(d)
    st.subheader(f"資料の取り込み：{info['name']}")

    job = current_job(d)
    busy = running_job()
    if job and job.status == "running":
        _progress(d)
        return

    # 追加のアップロード
    more = st.file_uploader("資料を追加する（PDF、複数可）", type=["pdf"], accept_multiple_files=True,
                            key=f"more-{d.name}-{st.session_state.get('upload_epoch', 0)}")
    if more and st.button("追加する"):
        n = _add_files(d, more)
        st.session_state["upload_epoch"] = st.session_state.get("upload_epoch", 0) + 1
        st.toast(f"{n} 件を追加しました")
        st.rerun()

    files = course_info(d)["files"]
    if not files:
        st.info("まだ資料がありません。上からアップロードしてください。")
        return

    st.markdown("**講義回の割り当て**　講義回が空欄のものや、講義資料ではないもの（読書課題など）は「使う」を外してください。")
    df = pd.DataFrame([{"使う": f["use"], "講義回": f["lecture"], "ファイル": f["name"]} for f in files])
    edited = st.data_editor(
        df, hide_index=True, use_container_width=True, key=f"files-{d.name}",
        column_config={
            "使う": st.column_config.CheckboxColumn(width="small"),
            "講義回": st.column_config.NumberColumn(min_value=1, max_value=99, step=1, width="small"),
            "ファイル": st.column_config.TextColumn(disabled=True),
        },
        disabled=["ファイル"],
    )
    new_files = []
    for _, row in edited.iterrows():
        n = None if pd.isna(row["講義回"]) else int(row["講義回"])
        new_files.append({"name": row["ファイル"], "lecture": n, "use": bool(row["使う"]) and n is not None})
    if new_files != files:
        info = course_info(d)
        info["files"] = new_files
        save_course_info(d, info)

    used = [f for f in new_files if f["use"]]
    dup = lecture_numbers_conflict(new_files)
    if dup:
        st.error("同じ講義回が重なっています: " + "、".join(f"第{n}回" for n in dup))

    has_vocab = (d / "vocab.json").exists()
    rebuild = False
    if has_vocab:
        rebuild = st.checkbox("語彙（概念の種類と関係の種類）も作り直す", value=False,
                              help="既定では前回の語彙を使い回す。使い回すと、取り込み直しても結果を比べられる（§4）")

    minutes = max(1, round(len(used) * SECONDS_PER_LECTURE / 60))
    st.caption(f"{len(used)} 回分を取り込みます。目安 {minutes} 分。処理中も画面は操作できます。")
    if busy and busy.course_dir != str(d):
        st.warning("別のコースの取り込みが実行中です。終わってから始めてください。")
    label = "取り込み直す" if (d / "graph.json").exists() else "取り込みを始める"
    if st.button(label, type="primary", disabled=bool(dup or not used or busy)):
        try:
            start_ingest(d, rebuild_vocab=rebuild)
        except ValueError as e:
            st.error(str(e))
            return
        st.rerun()

    if job and job.status == "error":
        st.error(f"前回の取り込みは失敗しました: {job.error}")
        with st.expander("ログ"):
            st.code("\n".join(job.log[-40:]))
    elif job and job.status == "done":
        st.success(f"前回の取り込みは完了しています（{job.finished_at}）。")


@st.fragment(run_every="2s")
def _progress(d: Path) -> None:
    job = current_job(d)
    if job is None:
        return
    if job.status != "running":
        st.rerun(scope="app")          # 終わったら画面全体を描き直す（地図を開けるようにする）
    st.progress(min(1.0, job.progress), text=f"{PHASE_LABEL.get(job.phase, job.phase)}…")
    st.caption(f"開始 {job.started_at}。この画面を開いたままでも、ほかの操作をしても処理は続きます。")
    st.code("\n".join(job.log[-12:]) or "開始しています…")
