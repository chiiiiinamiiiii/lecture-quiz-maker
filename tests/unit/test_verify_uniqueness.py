"""quizmap.quiz.verify_uniqueness（出題の検証、docs/rag-design.md）と generate_quiz からの呼び出し。

LLM は quiz.call_json を差し替え、SlideIndex と Wiki は偽物を渡す。API は呼ばない。
"""
from __future__ import annotations

import pytest

from quizmap import quiz

PAGES = [
    {"lecture": 1, "page": 1, "text": "甲野は乙説を唱えた。"},
    {"lecture": 1, "page": 2, "text": "丙は丁説を唱えた。戊も登場する。"},
]

GRAPH = {
    "concepts": [
        {"id": "kouno", "label": "甲野", "kind": "人物", "definition": "", "sources": [{"lecture": 1, "page": 1}]},
        {"id": "otsu", "label": "乙説", "kind": "学説", "definition": "", "sources": [{"lecture": 1, "page": 1}]},
        {"id": "hei", "label": "丙", "kind": "人物", "definition": "", "sources": [{"lecture": 1, "page": 2}]},
        {"id": "tei", "label": "丁説", "kind": "学説", "definition": "", "sources": [{"lecture": 1, "page": 2}]},
    ],
    "relations": [
        {"from": "kouno", "to": "otsu", "label": "提唱", "source": {"lecture": 1, "page": 1}, "origin": "slide"},
        {"from": "hei", "to": "otsu", "label": "提唱", "source": {"lecture": 1, "page": 2}, "origin": "external",
         "evidence": "x", "evidence_url": "u"},
    ],
}


def mcq(qid="q_01", choices=("甲野", "丙", "戊", "己"), answer=0, aspect="definition", used=("", "", "")):
    return {"id": qid, "type": "mcq", "aspect": aspect, "prompt": "乙説を唱えたのは誰か。", "choices": list(choices),
            "answer": answer, "answer_text": choices[answer], "explanation": "", "concept_labels": ["乙説"],
            "relation_used": dict(zip(("from", "to", "label"), used)), "sources": [{"lecture": 1, "page": 1}]}


def short(qid="q_02"):
    return {"id": qid, "type": "short", "aspect": "definition", "prompt": "乙説を唱えたのは誰か。", "choices": [],
            "answer": 0, "answer_text": "甲野", "explanation": "", "concept_labels": ["乙説"],
            "relation_used": {"from": "", "to": "", "label": ""}, "sources": [{"lecture": 1, "page": 1}]}


def check(qid, supported=True, support=((1, 1),), quote="甲野は乙説を唱えた", distractors=(), other=False):
    return {"id": qid, "answer_supported": supported, "support": [{"lecture": l, "page": p} for l, p in support],
            "quote": quote, "distractors": list(distractors), "other_answer": other, "reason_other": "別解" if other else ""}


def D(choice, bad=False, replacement="", wiki=0):
    return {"choice": choice, "could_be_correct": bad, "reason": "理由", "wiki": wiki, "replacement": replacement}


class FakeIndex:
    def __init__(self, hits):
        self.hits, self.queries = hits, []

    def search(self, client, query, k=5, exclude=frozenset()):
        self.queries.append(query)
        return self.hits[:k]


class FakeWiki:
    def __init__(self, articles):
        self.articles, self.terms = articles, []

    def lookup(self, term):
        self.terms.append(term)
        return self.articles.get(term)


@pytest.fixture
def llm(monkeypatch):
    state = {"checks": [], "users": [], "calls": 0}

    def fake(client, *, model, system, user, schema, max_tokens=32000):
        state["users"].append(user)
        state["calls"] += 1
        return {"checks": state["checks"]}

    monkeypatch.setattr(quiz, "call_json", fake)
    return state


def run(qs, index=None, wiki=None):
    return quiz.verify_uniqueness(None, "m", qs, GRAPH, PAGES, index, wiki)


def test_supported_question_is_kept_and_one_call(llm):
    llm["checks"] = [check("q_01", distractors=[D(1), D(2), D(3)])]
    kept, rec = run([mcq()])
    assert [q["id"] for q in kept] == ["q_01"] and llm["calls"] == 1
    assert rec[0]["action"] == "kept" and rec[0]["support"] == [{"lecture": 1, "page": 1}]
    assert rec[0]["pages"] == [{"lecture": 1, "page": 1, "score": None, "via": "source"}]
    assert len(rec[0]["distractors"]) == 3


def test_unsupported_or_bad_quote_is_dropped(llm):
    llm["checks"] = [check("q_01", supported=False), check("q_02", quote="甲野は乙説を否定した")]
    kept, rec = run([mcq("q_01"), mcq("q_02")])
    assert kept == []
    assert rec[0]["reasons"] == ["正答が資料の記述から導けない"]
    assert rec[1]["reasons"] == ["正答の根拠の引用が、挙げた資料のページに無い"]


def test_support_page_must_be_one_shown(llm):
    llm["checks"] = [check("q_01", support=((1, 2),), quote="丙は丁説を唱えた")]
    kept, rec = run([mcq()])
    assert kept == [] and rec[0]["action"] == "dropped"


def test_distractor_replaced_with_slide_term_or_dropped(llm):
    wiki = FakeWiki({"丙": {"title": "丙", "url": "https://ja.wikipedia.org/wiki/丙", "extract": "丙は乙説も唱えた。"}})
    llm["checks"] = [check("q_01", distractors=[D(1, True, "戊", wiki=0), D(2), D(3)]),
                     check("q_02", distractors=[D(1, True, "資料に無い語"), D(2), D(3)])]
    qs = [mcq("q_01", choices=("甲野", "丙", "丁説", "己")), mcq("q_02")]
    kept, rec = run(qs, wiki=wiki)
    assert [q["choices"] for q in kept] == [["甲野", "戊", "丁説", "己"]]
    assert kept[0]["answer_text"] == "甲野" and qs[0]["choices"][1] == "丙"     # 元の問題は書き換えない
    assert rec[0]["action"] == "replaced" and rec[0]["distractors"][0]["replaced"] is True
    assert rec[1]["action"] == "dropped"
    assert {"term": "丙", "title": "丙", "url": "https://ja.wikipedia.org/wiki/丙"} in rec[0]["wiki"]
    assert {"term": "己", "title": "", "url": ""} in rec[0]["wiki"]
    assert wiki.terms.count("丙") == 1                                  # 同じ語は 1 度だけ引く


def test_wiki_url_recorded_for_distractor(llm):
    wiki = FakeWiki({"丙": {"title": "丙", "url": "U丙", "extract": "丙は乙説も唱えた。"}})
    llm["checks"] = [check("q_01", distractors=[D(1, True, "戊", wiki=1)])]
    _, rec = run([mcq()], wiki=wiki)
    assert rec[0]["distractors"][0]["wiki_url"] == "U丙"
    assert "[W1] 丙" in llm["users"][0]


def test_short_with_other_answer_is_dropped(llm):
    llm["checks"] = [check("q_02", other=True)]
    kept, rec = run([short()])
    assert kept == [] and rec[0]["reason_other"] == "別解"


def test_external_relation_question_dropped_without_llm(llm):
    q = mcq(aspect="relation", used=("丙", "乙説", "提唱"))
    kept, rec = run([q])
    assert kept == [] and llm["calls"] == 0
    assert rec[0]["reasons"] == [quiz.EXTERNAL_RELATION_REASON]


def test_index_pages_are_added_with_score(llm):
    index = FakeIndex([{"lecture": 1, "page": 2, "text": "", "score": 0.8}, {"lecture": 9, "page": 9, "text": "", "score": 0.1}])
    llm["checks"] = [check("q_01", support=((1, 2),), quote="戊も登場する")]
    kept, rec = run([mcq()], index=index)
    assert [p["via"] for p in rec[0]["pages"]] == ["source", "search"]
    assert rec[0]["pages"][1]["score"] == 0.8 and len(kept) == 1


def test_missing_check_keeps_question(llm):
    llm["checks"] = []
    kept, rec = run([mcq()])
    assert len(kept) == 1 and "返らなかった" in rec[0]["reasons"][0]


def test_materials_exclude_external_relations():
    vocab = {"relations": [{"label": "提唱", "definition": "", "question_style": "", "example": ""}]}
    _, _, inner, _, _, _, _ = quiz.build_materials(GRAPH, vocab, PAGES, ["kouno", "otsu", "hei"])
    assert [(r["from"], r["to"]) for r in inner] == [("kouno", "otsu")]


def test_generate_quiz_calls_verify_and_keeps_before(monkeypatch):
    q = mcq()
    seen = {"verify": []}

    def fake(client, *, model, system, user, schema, max_tokens=32000):
        if "checks" in schema["properties"]:
            seen["verify"].append(user)
            return {"checks": [check("q_01", supported=False)]}
        if "distractor_reasons" in str(schema):
            return {"questions": [{**q, "distractor_reasons": []}]}
        return {"questions": [q]}

    monkeypatch.setattr(quiz, "call_json", fake)
    vocab = {"meta": {"language": "ja"}, "relations": [{"label": "提唱", "definition": "", "question_style": "", "example": ""}]}
    out = quiz.generate_quiz(None, "m", GRAPH, vocab, PAGES, ["kouno", "otsu"], 1, verify=True)
    assert out["questions"] == [] and len(seen["verify"]) == 1
    assert [x["id"] for x in out["meta"]["questions_before_verify"]] == ["q_01"]
    assert out["meta"]["uniqueness"]["dropped"] == 1 and out["meta"]["api_calls"] == 3

    out = quiz.generate_quiz(None, "m", GRAPH, vocab, PAGES, ["kouno", "otsu"], 1, verify=False)
    assert len(out["questions"]) == 1 and "uniqueness" not in out["meta"] and out["meta"]["api_calls"] == 2
