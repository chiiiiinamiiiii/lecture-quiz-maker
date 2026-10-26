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


def check(qid, supported=True, support=((1, 1),), quote="甲野は乙説を唱えた", distractors=(), other=False,
          same_as="", conflicts_with=""):
    return {"id": qid, "answer_supported": supported, "support": [{"lecture": l, "page": p} for l, p in support],
            "quote": quote, "distractors": list(distractors), "other_answer": other, "reason_other": "別解" if other else "",
            "same_as": same_as, "conflicts_with": conflicts_with}


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


def test_unsupported_is_dropped_but_bad_quote_is_kept(llm):
    """外すのは LLM が導けないと判定したときだけ。引用の照合に落ちただけでは外さない（#91）。"""
    llm["checks"] = [check("q_01", supported=False), check("q_02", quote="甲野は乙説を否定した")]
    kept, rec = run([mcq("q_01"), mcq("q_02")])
    assert [q["id"] for q in kept] == ["q_02"]
    assert rec[0]["reasons"] == ["正答が資料の記述から導けない"] and rec[0]["action"] == "dropped"
    assert rec[1]["reasons"] == ["引用の照合に失敗（残した）"] and rec[1]["quote_check"] == "failed"
    assert rec[1]["action"] == "kept"


def test_quote_matched_on_any_shown_page_and_support_rewritten(llm):
    """support に挙げたページが違っても、見せたページのどれかに引用があれば通し、support を入れ直す（#91）。"""
    index = FakeIndex([{"lecture": 1, "page": 2, "text": "", "score": 0.8}])
    llm["checks"] = [check("q_01", support=((1, 1),), quote="丙は丁説を唱えた")]
    kept, rec = run([mcq()], index=index)
    assert len(kept) == 1 and rec[0]["quote_check"] == "ok"
    assert rec[0]["support"] == [{"lecture": 1, "page": 2}]
    assert rec[0]["support_claimed"] == [{"lecture": 1, "page": 1}]


def test_quote_fragments_split_by_ellipsis():
    page_text = {(1, 1): "甲野は乙説を唱えた。", (1, 2): "丙は丁説を唱えた。戊も登場する。"}
    keys = [(1, 1), (1, 2)]
    assert quiz.match_quote("甲野は乙説を唱えた…丙は丁説を唱えた", keys, page_text) == [(1, 1), (1, 2)]
    assert quiz.match_quote("甲野は乙説を唱えた...丙は丁説を唱え", keys, page_text) == [(1, 1), (1, 2)]
    assert quiz.match_quote("甲野は乙説を唱えた……戊も", keys, page_text) == [(1, 1)]   # 6 字未満の断片は見ない
    assert quiz.match_quote("甲野は乙説を唱えた…丙は乙説を唱えた", keys, page_text) is None
    assert quiz.match_quote("甲野は", keys, page_text) is None                            # 断片が全部短い
    assert quiz.match_quote("丙は丁説を唱えた", [(1, 1)], page_text) is None               # 見せていないページ


def test_overlapping_question_dropped_and_conflict_recorded(llm):
    llm["checks"] = [check("q_01"), check("q_02", same_as="q_01"), check("q_03", conflicts_with="q_01")]
    kept, rec = run([mcq("q_01"), mcq("q_02"), mcq("q_03")])
    assert [q["id"] for q in kept] == ["q_01", "q_03"]
    assert rec[1]["action"] == "dropped" and rec[1]["reasons"] == ["q_01 と重なる問題"]
    assert rec[2]["action"] == "kept" and "q_01 と正答が食い違う（残した）" in rec[2]["reasons"]


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
    assert all(w["term"] != "己" for w in rec[0]["wiki"])                # 概念に当たらない選択肢は引かない（#93）
    assert {"term": "丁説", "title": "", "url": ""} in rec[0]["wiki"]
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


def test_wiki_terms_are_concept_labels_not_choice_sentences(llm):
    """Wikipedia で引くのは、選択肢・正答に出てくる概念の label だけ（#93）。"""
    wiki = FakeWiki({})
    q = mcq(choices=("甲野が乙説を唱えた", "丙が丁説を唱えた", "誰も唱えていない", "己"))
    llm["checks"] = [check("q_01")]
    _, rec = run([q], wiki=wiki)
    assert wiki.terms == ["甲野", "乙説", "丙", "丁説"]
    assert [w["term"] for w in rec[0]["wiki"]] == ["甲野", "乙説", "丙", "丁説"]


def test_concept_terms_skip_label_inside_longer_label():
    graph = {"concepts": [{"label": "尊厳"}, {"label": "尊厳性"}, {"label": "人格"}]}
    assert quiz.concept_terms(["人格の尊厳性"], graph) == ["人格", "尊厳性"]
    assert quiz.concept_terms(["関係の無い文"], graph) == []


COOC_GRAPH = {**GRAPH, "relations": GRAPH["relations"] + [
    {"from": "tei", "to": "hei", "label": "関連", "source": {"lecture": 1, "page": 2}, "origin": "cooccurrence",
     "added_by": "connect", "via": "same_page"}]}


def test_materials_exclude_cooccurrence_relations():
    """近くに出るだけでつないだ「関連」（origin: cooccurrence、#127）は、関係を問う問題の材料にしない。"""
    vocab = {"relations": [{"label": "提唱", "definition": "", "question_style": "", "example": ""}]}
    _, _, inner, _, _, _, _ = quiz.build_materials(COOC_GRAPH, vocab, PAGES, ["hei", "tei"])
    assert inner == []


def test_cooccurrence_relation_is_treated_like_external():
    q = mcq(aspect="relation", used=("丁説", "丙", "関連"))
    assert quiz.uses_external_relation(q, COOC_GRAPH)
    assert not quiz.uses_external_relation(mcq(aspect="relation", used=("甲野", "乙説", "提唱")), COOC_GRAPH)


def test_generate_quiz_drops_relation_question_with_reversed_answer(monkeypatch):
    """正答になる端（answer_end、#156）と正答が逆の関係の問題は外し、数を meta に残す。"""
    style = {"label": "提唱", "definition": "from が人物、to が学説", "question_style": "A（to）を唱えた人物（from）を問う",
             "example": "", "template": "{from}は{to}を唱えた", "answer_end": "from"}
    right = mcq(aspect="relation", used=("甲野", "乙説", "提唱"))                     # 正答は 甲野（from）
    wrong = {**mcq(qid="q_02", choices=("乙説", "丙", "戊", "己"), aspect="relation", used=("甲野", "乙説", "提唱"))}

    def fake(client, *, model, system, user, schema, max_tokens=32000):
        assert "「甲野は乙説を唱えた」" in user and "正答は「甲野」" in user
        return {"questions": [{**right, "distractor_reasons": []}, {**wrong, "distractor_reasons": []}]}
    monkeypatch.setattr(quiz, "call_json", fake)
    vocab = {"meta": {"language": "ja"}, "relations": [style]}
    out = quiz.generate_quiz(None, "m", GRAPH, vocab, PAGES, ["kouno", "otsu"], 2, verify=False)
    assert [x["id"] for x in out["questions"]] == ["q_01"]
    assert out["meta"]["dropped_relation_direction"] == 1
    assert any("q_02" in w and "外した" in w for w in out["warnings"])
