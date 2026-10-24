"""§7 クイズ生成、§8 解答・ヒント・採点、結果の保存。右側のパネルに出す。"""
import json

import streamlit as st

from quizmap.common import DEFAULT_MODEL, make_client
from quizmap.grade import grade, save_results
from quizmap.quiz import generate_quiz
from ui.i18n import t
from ui.state import course_dir, load_pages

STATE_KEYS = ("graded", "hints", "results_saved", "grade_calls")


def quiz_section(graph, vocab, ids: list[str]) -> None:
    """§7 クイズ生成 → §8 解答・ヒント・採点 → 結果の保存 → §9 出題済みを地図に反映。"""
    st.divider()
    st.subheader(t("quiz_title"))
    result = st.session_state.get("quiz")
    if not result:
        col_n, col_b = st.columns([1, 2], vertical_alignment="bottom")
        n_q = col_n.number_input(t("n_questions"), min_value=1, max_value=20, value=5, step=1)
        if col_b.button(t("generate"), type="primary", use_container_width=True, disabled=not ids):
            _generate(graph, vocab, ids, int(n_q))
        return

    m = result["meta"]
    st.caption(t("quiz_meta", n=m["n_generated"], r=m["distractors_replaced"], s=m["seconds"]))
    # 自動検証の警告（誤答の差し替えなど）は学習者向けではないので画面に出さない。生成結果の JSON には残る
    questions = result["questions"]
    if "graded" in st.session_state:
        review_section(questions, st.session_state["graded"])
    else:
        answer_section(questions)
    if st.button(t("new_quiz"), use_container_width=True):
        st.session_state.pop("quiz", None)
        _clear_answer_state()
        st.rerun()


def _clear_answer_state() -> None:
    for k in STATE_KEYS:
        st.session_state.pop(k, None)
    for k in [k for k in st.session_state if k.startswith(("ans-", "hint-"))]:
        del st.session_state[k]


def _generate(graph, vocab, ids, n) -> None:
    with st.spinner(t("generating")):
        try:
            result = generate_quiz(make_client(), DEFAULT_MODEL, graph, vocab, load_pages(), ids, n)
        except Exception as e:  # noqa: BLE001  画面に理由を出して止める
            st.error(t("gen_failed", e=e))
            return
    st.session_state["quiz"] = result
    _clear_answer_state()
    (course_dir() / "quiz_latest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    st.rerun()


def _source_text(q) -> str:
    return "、".join(t("lecture_fmt", n=s["lecture"]) + f" p.{s['page']}" for s in q["sources"])


def _slide_images(q) -> list:
    imgs = [course_dir() / "slides" / f"L{s['lecture']}_p{s['page']}.jpg" for s in q["sources"]]
    return [p for p in imgs if p.exists()]


def answer_section(questions) -> None:
    """§8 解答。全問まとめて「採点する」。解答中に正解は見せない。

    ヒントは答えを言わない。仕様 §8 のとおり、押すまで出典のスライドを伏せておき、押すと見せる
    （概念の定義を見せると、定義を問う問題では答えそのものになる: #15）。使ったことは hint_used に記録する。
    """
    hints = st.session_state.setdefault("hints", {})
    for i, q in enumerate(questions, start=1):
        with st.container(border=True):
            st.markdown(f"**{t('q_label', i=i)}**　{q['prompt']}")
            if q["type"] == "mcq":
                st.radio(t("q_label", i=i), options=list(range(len(q["choices"]))),
                         format_func=lambda k, ch=q["choices"]: ch[k],
                         index=None, key=f"ans-{q['id']}", label_visibility="collapsed")
            else:
                st.text_input(t("answer_short"), key=f"ans-{q['id']}", placeholder=t("answer_placeholder"))
            if st.button(t("hint_btn"), key=f"hint-{q['id']}", type="tertiary"):
                hints[q["id"]] = True
            if hints.get(q["id"]):
                st.caption(f"{t('hint_source')}: {_source_text(q)}")
                for img in _slide_images(q)[:2]:
                    st.image(str(img), use_container_width=True)

    if st.button(t("grade"), type="primary", use_container_width=True):
        answers = {q["id"]: st.session_state.get(f"ans-{q['id']}") for q in questions}
        page_text = {(p["lecture"], p["page"]): p["text"] for p in load_pages()}
        with st.spinner(t("grading")):
            try:
                graded, calls = grade(make_client(), DEFAULT_MODEL, questions, answers, page_text)
            except Exception as e:  # noqa: BLE001
                st.error(t("grade_failed", e=e))
                return
        st.session_state["graded"] = graded
        st.session_state["grade_calls"] = calls
        if not st.session_state.get("results_saved"):
            save_results(course_dir() / "results.json", questions, graded, hints)   # §8 保存: results.json に追記
            st.session_state["results_saved"] = True
            # §9 出題済みの地図への反映は、results.json の更新を state.load_graph が検知して行う
        st.rerun()


def review_section(questions, graded) -> None:
    """採点後: 正誤・解説・出典・スライド画像を表示する。"""
    total = sum(g["score"] for g in graded.values())
    hints = st.session_state.get("hints", {})
    st.success(f"{t('score', t=f'{total:g}', n=len(questions))}　{t('saved_note')}")
    for i, q in enumerate(questions, start=1):
        g = graded[q["id"]]
        mark = "○" if g["score"] >= 1.0 else ("△" if g["score"] > 0 else "×")
        with st.container(border=True):
            st.markdown(f"**{mark} {t('q_label', i=i)}**　{q['prompt']}"
                        + (f"　_({t('hint_used')})_" if hints.get(q["id"]) else ""))
            if q["type"] == "mcq":
                ua = g["user_answer"]
                st.markdown(f"{t('your_answer')}: {q['choices'][ua] if ua is not None else t('no_answer')}  \n"
                            f"{t('correct_answer')}: **{q['choices'][q['answer']]}**")
            else:
                st.markdown(f"{t('your_answer')}: {g['user_answer'] or t('no_answer')}  \n"
                            f"{t('model_answer')}: **{q['answer_text']}**")
                if g["feedback"]:
                    st.caption(f"{t('feedback')}: {g['feedback']}")
            st.markdown(f"{t('explanation')}: {q['explanation']}")
            st.caption(f"{t('hint_source')}: {_source_text(q)}")
            for img in _slide_images(q)[:2]:
                st.image(str(img), use_container_width=True)
    with st.expander(t("json_expander")):
        st.json(questions, expanded=False)
