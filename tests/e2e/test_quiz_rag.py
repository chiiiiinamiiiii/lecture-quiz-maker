"""出題の画面から RAG の索引と Wikipedia を出題の関数に渡す（#78）。API は呼ばない（出題の関数を差し替える）。"""
from pathlib import Path

import ui.quiz_view as qv


def _patch(monkeypatch, rag, questions=("q",)):
    sent = {}
    monkeypatch.setattr(qv, "course_dir", lambda: Path("/nonexistent/course"))
    monkeypatch.setattr(qv, "open_rag", lambda d: (sent.setdefault("rag_dir", d), rag)[1])
    monkeypatch.setattr(qv, "make_client", lambda: "client")
    monkeypatch.setattr(qv, "load_pages", lambda: ["page"])

    def fake_generate(client, model, graph, vocab, pages, ids, n, **kw):
        sent.update(client=client, pages=pages, ids=ids, n=n, kw=kw)
        return {"questions": list(questions), "warnings": [], "meta": {"n_generated": len(questions)}}

    monkeypatch.setattr(qv, "generate_quiz", fake_generate)
    return sent


def test_index_and_wiki_are_passed_to_generate_quiz(monkeypatch):
    sent = _patch(monkeypatch, ("INDEX", "WIKI"))
    qv.make_quiz({"g": 1}, {"v": 1}, ["c1"], 5)
    assert sent["rag_dir"] == Path("/nonexistent/course")
    assert sent["kw"] == {"index": "INDEX", "wiki": "WIKI"}      # verify は省き、QUIZMAP_RAG に任せる
    assert sent["ids"] == ["c1"] and sent["n"] == 5 and sent["pages"] == ["page"]


def test_rag_off_passes_none(monkeypatch):
    sent = _patch(monkeypatch, (None, None))
    qv.make_quiz({}, {}, ["c1"], 3)
    assert sent["kw"] == {"index": None, "wiki": None}


def test_spinner_mentions_verification():
    from ui.i18n import TEXTS
    assert "正しさの確認" in TEXTS["generating"]["ja"]
    assert "external sources" in TEXTS["generating"]["en"]
    assert TEXTS["no_valid_questions"]["ja"] and TEXTS["no_valid_questions"]["en"]


def test_quiz_count_is_the_actual_number(monkeypatch):
    """確認で外されて頼んだ数より少ないときも、画面の問題数は実際の数（meta の n_generated）。"""
    import streamlit as st
    from ui.i18n import t
    shown = []
    monkeypatch.setattr(st, "caption", lambda s: shown.append(s))
    monkeypatch.setattr(qv, "answer_section", lambda qs: None)
    monkeypatch.setattr(st, "button", lambda *a, **k: False)
    monkeypatch.setattr(st, "divider", lambda: None)
    monkeypatch.setattr(st, "subheader", lambda s: None)
    st.session_state["quiz"] = {"questions": ["a", "b", "c"], "meta": {"n_requested": 5, "n_generated": 3}}
    try:
        qv.quiz_section({}, {}, ["c1"])
    finally:
        st.session_state.pop("quiz", None)
    assert shown == [t("quiz_meta", n=3)]
