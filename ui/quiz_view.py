"""§7 クイズ生成、§8 解答・ヒント・採点、結果の保存。"""
import json

import streamlit as st

from quizmap.common import DEFAULT_MODEL, make_client
from quizmap.grade import grade, save_results
from quizmap.quiz import generate_quiz
from ui.state import course_dir, load_pages


def quiz_section(graph, vocab, ids: list[str]) -> None:
    """§7 クイズ生成 → §8 解答・ヒント・採点 → 結果の保存 → §9 出題済みを地図に反映。"""
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
        # 新しいクイズでは解答・ヒント・採点の状態を捨てる
        for k in ("graded", "hints", "results_saved"):
            st.session_state.pop(k, None)
        for k in [k for k in st.session_state if k.startswith("ans-")]:
            del st.session_state[k]
        (course_dir() / "quiz_latest.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = st.session_state.get("quiz")
    if not result:
        return
    m = result["meta"]
    st.caption(f"{m['n_generated']} 問 / 内訳 {m['aspect_actual']} / 形式 {m['type_actual']} / "
               f"誤答差し替え {m['distractors_replaced']} 件 / API {m['api_calls']} 回 / {m['seconds']} 秒")
    for w in result["warnings"]:
        st.warning(w)

    questions = result["questions"]
    if "graded" in st.session_state:
        review_section(graph, questions, st.session_state["graded"])
    else:
        answer_section(graph, questions)


def hint_text(graph, q) -> str:
    """ヒントは答えを言わない。根拠となる概念の定義と出典だけ。"""
    concepts = {c["id"]: c for c in graph["concepts"]}
    lines = []
    for cid in q["concepts"]:
        c = concepts.get(cid)
        if c:
            lines.append(f"- **{c['label']}**（{c['kind']}）: {c['definition']}")
    src = "、".join(f"第{s['lecture']}回 p.{s['page']}" for s in q["sources"])
    lines.append(f"- 出典: {src}")
    return "\n".join(lines)


def answer_section(graph, questions) -> None:
    """§8 解答 UI。全問まとめて「採点する」。解答中に正解は見せない。"""
    st.subheader("解答")
    hints = st.session_state.setdefault("hints", {})
    for i, q in enumerate(questions, start=1):
        st.markdown(f"**問 {i}.** {q['prompt']}")
        if q["type"] == "mcq":
            st.radio("選択肢", options=list(range(len(q["choices"]))),
                     format_func=lambda k, ch=q["choices"]: ch[k],
                     index=None, key=f"ans-{q['id']}", label_visibility="collapsed")
        else:
            st.text_input("解答（記述）", key=f"ans-{q['id']}", placeholder="ここに答えを書く")
        # ヒント: 押したことを記録する（hint_used）。押すまで中身は見せない
        if st.button("ヒントを見る", key=f"hint-{q['id']}"):
            hints[q["id"]] = True
        if hints.get(q["id"]):
            st.info(hint_text(graph, q))
        st.divider()

    if st.button("採点する", type="primary"):
        answers = {q["id"]: st.session_state.get(f"ans-{q['id']}") for q in questions}
        page_text = {(p["lecture"], p["page"]): p["text"] for p in load_pages()}
        with st.spinner("採点中…"):
            try:
                graded, calls = grade(make_client(), DEFAULT_MODEL, questions, answers, page_text)
            except Exception as e:  # noqa: BLE001
                st.error(f"採点に失敗: {e}")
                return
        st.session_state["graded"] = graded
        st.session_state["grade_calls"] = calls
        if not st.session_state.get("results_saved"):
            save_results(course_dir() / "results.json", questions, graded, hints)      # §8 保存: results.json に追記
            st.session_state["results_saved"] = True
            # §9 出題済みの地図への反映は、results.json の更新を state.load_graph が検知して行う
        st.rerun()


def review_section(graph, questions, graded) -> None:
    """採点後: 正誤・解説・出典・スライド画像を表示する。"""
    st.subheader("採点結果")
    total = sum(g["score"] for g in graded.values())
    hints = st.session_state.get("hints", {})
    st.success(f"{total:g} / {len(questions)} 点（記述の採点 API 呼び出し {st.session_state.get('grade_calls', 0)} 回）。"
               f" 結果を保存し、出題済みの概念を地図に反映しました。")
    for i, q in enumerate(questions, start=1):
        g = graded[q["id"]]
        mark = "○" if g["score"] >= 1.0 else ("△" if g["score"] > 0 else "×")
        st.markdown(f"**問 {i}.** {q['prompt']}　**{mark} {g['score']:g} 点**"
                    + ("　（ヒント使用）" if hints.get(q["id"]) else ""))
        if q["type"] == "mcq":
            ua = g["user_answer"]
            st.write("あなたの答え:", q["choices"][ua] if ua is not None else "（無回答）",
                     "／ 正答:", q["choices"][q["answer"]])
        else:
            st.write("あなたの答え:", g["user_answer"] or "（無回答）", "／ 模範解答:", q["answer_text"])
            if g["feedback"]:
                st.write("採点コメント:", g["feedback"])
        st.markdown(f"解説: {q['explanation']}")
        src = "、".join(f"第{s['lecture']}回 p.{s['page']}" for s in q["sources"])
        st.caption(f"出典: {src}")
        imgs = [course_dir() / "slides" / f"L{s['lecture']}_p{s['page']}.jpg" for s in q["sources"]]
        imgs = [p for p in imgs if p.exists()]
        if imgs:
            cols = st.columns(3)                       # 画像は 3 列幅に収める（1 枚でも横いっぱいにしない）
            for k, img in enumerate(imgs[:3]):
                cols[k].image(str(img), caption=img.stem, use_container_width=True)
        st.divider()
    with st.expander("生成結果の JSON（答えを含む）"):
        st.json(questions, expanded=False)
