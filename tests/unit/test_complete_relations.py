"""quizmap.extraction.complete_relations（関係の補完、docs/rag-design.md）。

LLM は extraction.call_json を差し替え、SlideIndex と Wiki は偽物を渡す。API は呼ばない。
"""
from __future__ import annotations

import pytest

from quizmap import extraction
from quizmap.common import OTHER_LABEL

VOCAB = {
    "course": "テスト",
    "kinds": [{"name": "人物"}, {"name": "学説"}],
    "relations": [
        {"label": "提唱", "definition": "人物が学説を唱えた", "example": "A → B",
         "from_kinds": ["人物"], "to_kinds": ["学説"]},
        {"label": OTHER_LABEL, "definition": "", "example": ""},
    ],
}

PAGES = [
    {"lecture": 1, "page": 1, "text": "甲野は乙説を唱えた。丙も出てくる。"},
    {"lecture": 1, "page": 2, "text": "丁説の説明。"},
    {"lecture": 2, "page": 1, "text": "丙と丁説の話。"},
]


def concept(cid, label, kind, *pages):
    return {"id": cid, "label": label, "kind": kind, "definition": "",
            "sources": [{"lecture": l, "page": p} for l, p in pages], "lectures": sorted({l for l, _ in pages})}


def graph():
    return {
        "course": "テスト",
        "concepts": [
            concept("kouno", "甲野", "人物", (1, 1)),
            concept("otsu", "乙説", "学説", (1, 1)),
            concept("hei", "丙", "人物", (1, 1)),
            concept("tei", "丁説", "学説", (1, 2)),
        ],
        "relations": [{"from": "hei", "to": "otsu", "label": OTHER_LABEL, "source": {"lecture": 1, "page": 1}}],
        "stats": {"concepts": 4, "relations": 1},
    }


class FakeIndex:
    def __init__(self, hits):
        self.hits, self.queries = hits, []

    def search(self, client, query, k=5, exclude=frozenset()):
        raise AssertionError("complete_relations は search を 1 件ずつ呼ばず search_many を使う（#84）")

    def search_many(self, client, queries, k=5, exclude=frozenset()):
        self.queries.append((list(queries), k, set(exclude)))
        return [[h for h in self.hits.get(q, []) if (h["lecture"], h["page"]) not in exclude][:k] for q in queries]


class FakeWiki:
    def __init__(self, articles):
        self.articles, self.terms = articles, []

    def lookup(self, term):
        self.terms.append(term)
        return self.articles.get(term)


@pytest.fixture
def llm(monkeypatch):
    """call_json を差し替える。answer(user) が judgements を返す。呼ばれたプロンプトを残す。"""
    state = {"answer": lambda user: [], "users": []}

    def fake(client, *, model, system, user, schema, max_tokens=32000):
        state["users"].append(user)
        return {"judgements": state["answer"](user)}

    monkeypatch.setattr(extraction, "call_json", fake)
    return state


def J(pair, origin, f="", t="", label="", lecture=0, page=0, wiki=0, evidence=""):
    """判定 1 件。origin で、資料の根拠（slide）か Wikipedia の根拠（external）の欄だけを埋める。"""
    slide, ext = origin == "slide", origin == "external"
    return {"pair": pair, "from": f, "to": t, "label": label if origin != "none" else "",
            "slide_found": slide, "lecture": lecture if slide else 0, "page": page if slide else 0,
            "slide_evidence": evidence if slide else "",
            "external_found": ext, "wiki": wiki if ext else 0, "wiki_evidence": evidence if ext else ""}


def JB(pair, f, t, label, lecture, page, slide_evidence, wiki, wiki_evidence):
    """資料と Wikipedia の両方の根拠を書いた判定。"""
    return {"pair": pair, "from": f, "to": t, "label": label,
            "slide_found": True, "lecture": lecture, "page": page, "slide_evidence": slide_evidence,
            "external_found": True, "wiki": wiki, "wiki_evidence": wiki_evidence}


def pair_no(user, a, b):
    """プロンプトの対の一覧から、a と b の対の番号を探す。"""
    for line in user.splitlines():
        head, _, rest = line.partition(". ")
        if head.isdigit() and rest.startswith(a + "（"):
            nxt = user.split(line, 1)[1].splitlines()[1]
            if nxt.strip().startswith("と " + b + "（"):
                return int(head)
    raise AssertionError(f"対 {a}-{b} がプロンプトに無い")


def test_slide_relation_is_added_with_origin_and_existing_get_slide(llm):
    def answer(user):
        n = pair_no(user, "kouno", "otsu")
        return [J(n, "slide", "kouno", "otsu", "提唱", 1, 1, evidence="甲野は乙説を唱えた")]
    llm["answer"] = answer
    g = extraction.complete_relations(None, "m", VOCAB, graph(), PAGES, index=None, wiki=None, log=lambda s: None)
    new = [r for r in g["relations"] if r["from"] == "kouno"]
    assert new == [{"from": "kouno", "to": "otsu", "label": "提唱", "source": {"lecture": 1, "page": 1},
                    "origin": "slide", "evidence": "甲野は乙説を唱えた", "added_by": "completion"}]
    assert all(r["origin"] == "slide" for r in g["relations"])
    c = g["stats"]["completion"]
    assert c["added_slide"] == 1 and c["added_external"] == 0 and c["api_calls"] == 1
    assert g["stats"]["relations"] == 2
    assert {x["id"] for x in g["concepts"]} == {"kouno", "otsu", "hei", "tei"}


def test_existing_pair_is_not_a_candidate(llm):
    extraction.complete_relations(None, "m", VOCAB, graph(), PAGES, index=None, wiki=None, log=lambda s: None)
    user = llm["users"][0]
    with pytest.raises(AssertionError):
        pair_no(user, "hei", "otsu")


def test_new_or_other_ids_are_rejected(llm):
    def answer(user):
        n = pair_no(user, "kouno", "otsu")
        return [J(n, "slide", "kouno", "shin", "提唱", 1, 1, evidence="甲野は乙説を唱えた")]
    llm["answer"] = answer
    g = extraction.complete_relations(None, "m", VOCAB, graph(), PAGES, index=None, wiki=None, log=lambda s: None)
    assert len(g["relations"]) == 1
    assert g["stats"]["completion"]["dropped"]["対に無い id"] == 1


def test_slide_evidence_must_be_in_page(llm):
    def answer(user):
        n = pair_no(user, "kouno", "otsu")
        return [J(n, "slide", "kouno", "otsu", "提唱", 1, 1, evidence="甲野は乙説を否定した")]
    llm["answer"] = answer
    g = extraction.complete_relations(None, "m", VOCAB, graph(), PAGES, index=None, wiki=None, log=lambda s: None)
    assert g["stats"]["completion"]["dropped"]["evidence が本文に無い"] == 1


def test_direction_is_fixed_by_end_kinds_and_other_label_is_not_added(llm):
    def answer(user):
        return [J(pair_no(user, "kouno", "otsu"), "slide", "otsu", "kouno", "提唱", 1, 1, evidence="甲野は乙説を唱えた"),
                J(pair_no(user, "hei", "kouno"), "slide", "hei", "kouno", OTHER_LABEL, 1, 1, evidence="丙も出てくる")]
    llm["answer"] = answer
    g = extraction.complete_relations(None, "m", VOCAB, graph(), PAGES, index=None, wiki=None, log=lambda s: None)
    c = g["stats"]["completion"]
    assert c["adjusted"] == {"向きを直した": 1}
    assert c["dropped"]["語彙にないラベル"] == 1
    assert [(r["from"], r["to"]) for r in g["relations"] if r["label"] == "提唱"] == [("kouno", "otsu")]


def test_isolated_concept_uses_index_and_external_needs_wiki_text(llm):
    # 丁説（tei）はどの関係にもつながらず、ほかの概念と同じページにも出ない。索引で第2回 p1 が見つかり、丙と対になる
    index = FakeIndex({"丁説": [{"lecture": 2, "page": 1, "text": "", "score": 0.9}]})
    g0 = graph()
    g0["concepts"][2]["sources"].append({"lecture": 2, "page": 1})
    wiki = FakeWiki({"丁説": {"title": "丁説", "url": "https://ja.wikipedia.org/wiki/丁説",
                             "extract": "丁説は丙が唱えた学説である。"}})

    def answer(user):
        n = pair_no(user, "hei", "tei")
        w = next(int(x.split("]")[0]) for x in user.split("[W")[1:] if x.split("]")[1].strip().startswith("丁説"))
        return [J(n, "external", "hei", "tei", "提唱", wiki=w, evidence="丁説は丙が唱えた学説である")]
    llm["answer"] = answer
    g = extraction.complete_relations(None, "m", VOCAB, g0, PAGES, index=index, wiki=wiki, log=lambda s: None)
    ext = [r for r in g["relations"] if r["origin"] == "external"]
    assert ext == [{"from": "hei", "to": "tei", "label": "提唱", "source": {"lecture": 2, "page": 1},
                    "origin": "external", "evidence": "丁説は丙が唱えた学説である",
                    "evidence_url": "https://ja.wikipedia.org/wiki/丁説", "evidence_title": "丁説",
                    "added_by": "completion"}]
    c = g["stats"]["completion"]
    assert c["searches"] >= 1 and len(index.queries) == 1 and "丁説" in index.queries[0][0]
    assert c["isolated_after"] < c["isolated_before"]


def test_external_rejects_text_not_in_article(llm):
    wiki = FakeWiki({"甲野": {"title": "甲野", "url": "u", "extract": "甲野は哲学者。"}})

    def answer(user):
        return [J(pair_no(user, "kouno", "otsu"), "external", "kouno", "otsu", "提唱", wiki=1,
                  evidence="甲野は乙説を唱えた")]
    llm["answer"] = answer
    g = extraction.complete_relations(None, "m", VOCAB, graph(), PAGES, index=None, wiki=wiki, log=lambda s: None)
    assert g["stats"]["completion"]["dropped"]["evidence が記事に無い"] == 1
    assert len(g["relations"]) == 1


def test_external_needs_both_names_in_article():
    art = {"title": "甲野", "extract": "甲野は哲学者で、多くの説を唱えた。"}
    assert extraction.check_external_evidence("多くの説を唱えた", ["甲野", "乙説"], art) == "両端の概念名が記事に無い"
    art2 = {"title": "甲野", "extract": "甲野は乙説を唱えた哲学者。"}
    assert extraction.check_external_evidence("乙説を唱えた", ["甲野", "乙説"], art2) is None


def test_max_pairs_and_batches(llm, monkeypatch):
    monkeypatch.setattr(extraction, "COMPLETION_BATCH", 1)
    g = extraction.complete_relations(None, "m", VOCAB, graph(), PAGES, index=None, wiki=None,
                                      log=lambda s: None, max_pairs=2)
    c = g["stats"]["completion"]
    assert c["judged"] == 2 and c["api_calls"] == 2 and c["candidates"] == 2
    assert c["dropped"]["答えの無い対"] == 2


def test_input_graph_is_not_modified(llm):
    g0 = graph()
    extraction.complete_relations(None, "m", VOCAB, g0, PAGES, index=None, wiki=None, log=lambda s: None)
    assert "origin" not in g0["relations"][0] and g0["stats"] == {"concepts": 4, "relations": 1}


def test_pair_records_for_evaluation(llm):
    index = FakeIndex({"丁説": [{"lecture": 2, "page": 1, "text": "", "score": 0.9}]})
    g0 = graph()
    g0["concepts"][2]["sources"].append({"lecture": 2, "page": 1})
    wiki = FakeWiki({"甲野": {"title": "甲野", "url": "U甲野", "extract": "甲野は哲学者。"}})

    def answer(user):
        return [J(pair_no(user, "kouno", "otsu"), "slide", "kouno", "otsu", "提唱", 1, 1, evidence="甲野は乙説を唱えた"),
                J(pair_no(user, "hei", "kouno"), "none"),
                J(pair_no(user, "hei", "tei"), "slide", "hei", "tei", "提唱", 2, 1, evidence="無い文")]
    llm["answer"] = answer
    g = extraction.complete_relations(None, "m", VOCAB, g0, PAGES, index=index, wiki=wiki, log=lambda s: None)
    recs = {(r["a"], r["b"]): r for r in g["stats"]["completion"]["pairs"]}
    added = recs[("kouno", "otsu")]
    assert added["result"] == "added" and added["relation"]["added_by"] == "completion"
    assert added["pages"] == [{"lecture": 1, "page": 1, "score": None, "via": "shared"}]
    assert {"term": "甲野", "title": "甲野", "url": "U甲野"} in added["wiki"]
    assert recs[("hei", "kouno")]["result"] == "none"
    bad = recs[("hei", "tei")]
    assert bad["result"] == "rejected" and bad["reason"] == "evidence が本文に無い"
    assert bad["pages"] == [{"lecture": 2, "page": 1, "score": 0.9, "via": "search"}]
    assert bad["judgement"]["slide_evidence"] == "無い文"


def test_isolated_concepts_use_one_embedding_call(llm, monkeypatch):
    """つながらない概念が N 個でも、索引の検索の埋め込みは 1 回（#84）。本物の SlideIndex で埋め込みだけ差し替える。"""
    import numpy as np

    from quizmap import rag

    calls = []

    def fake_embed(client, texts, task="RETRIEVAL_DOCUMENT"):
        calls.append((list(texts), task))
        return [[1.0, float(i % 3)] for i, _ in enumerate(texts)]

    monkeypatch.setattr(rag, "embed", fake_embed)
    pages = [{"lecture": 1, "page": n, "text": f"ページ{n}"} for n in range(1, 8)]
    index = rag.SlideIndex(pages, np.array([[1.0, float(n % 3)] for n in range(7)], dtype=np.float32))
    g0 = {"course": "テスト", "relations": [], "stats": {},
          "concepts": [concept(f"c{n}", f"概念{n}", "人物", (1, n)) for n in range(1, 8)]}
    g = extraction.complete_relations(None, "m", VOCAB, g0, pages, index=index, wiki=None, log=lambda s: None)
    assert len(calls) == 1 and calls[0][1] == "RETRIEVAL_QUERY"
    assert sorted(calls[0][0]) == [f"概念{n}" for n in range(1, 8)]
    c = g["stats"]["completion"]
    assert c["isolated_before"] == 7 and c["searches"] == 7
    # 自分のページは候補のページにしない（exclude を概念ごとに渡せないので、取った後に除いている）
    for rec in c["pairs"]:
        for p in rec["pages"]:
            assert p["via"] == "search"


def test_own_pages_excluded_after_search_many(llm):
    hits = [{"lecture": 1, "page": 2, "text": "", "score": 0.99}, {"lecture": 2, "page": 1, "text": "", "score": 0.5}]
    index = FakeIndex({"丁説": hits})
    g0 = graph()
    g0["concepts"][2]["sources"].append({"lecture": 2, "page": 1})
    g = extraction.complete_relations(None, "m", VOCAB, g0, PAGES, index=index, wiki=None, log=lambda s: None)
    queries, k, exclude = index.queries[0]
    assert exclude == set() and k == extraction.COMPLETION_SEARCH_K + 1
    rec = {(r["a"], r["b"]): r for r in g["stats"]["completion"]["pairs"]}[("hei", "tei")]
    assert rec["pages"] == [{"lecture": 2, "page": 1, "score": 0.5, "via": "search"}]


WIKI_KOUNO = {"甲野": {"title": "甲野", "url": "U甲野", "extract": "甲野は哲学者で、乙説を唱えたことで知られる。"}}


def test_external_used_when_slide_evidence_fails(llm):
    """資料の根拠が照合で落ちても、Wikipedia の根拠が通れば external で足す（#88）。"""
    def answer(user):
        return [JB(pair_no(user, "kouno", "otsu"), "kouno", "otsu", "提唱", 1, 1, "甲野は乙説を否定した",
                   1, "甲野は哲学者で、乙説を唱えたことで知られる")]
    llm["answer"] = answer
    g = extraction.complete_relations(None, "m", VOCAB, graph(), PAGES, index=None, wiki=FakeWiki(WIKI_KOUNO),
                                      log=lambda s: None)
    rec = {(r["a"], r["b"]): r for r in g["stats"]["completion"]["pairs"]}[("kouno", "otsu")]
    assert rec["result"] == "added" and rec["relation"]["origin"] == "external"
    assert rec["relation"]["evidence_url"] == "U甲野"
    assert rec["reason"] == "資料の根拠は通らなかった: evidence が本文に無い"
    assert rec["judgement"]["origin"] == "slide" and rec["judgement"]["external_found"] is True


def test_slide_preferred_when_both_pass(llm):
    def answer(user):
        return [JB(pair_no(user, "kouno", "otsu"), "kouno", "otsu", "提唱", 1, 1, "甲野は乙説を唱えた",
                   1, "甲野は哲学者で、乙説を唱えたことで知られる")]
    llm["answer"] = answer
    g = extraction.complete_relations(None, "m", VOCAB, graph(), PAGES, index=None, wiki=FakeWiki(WIKI_KOUNO),
                                      log=lambda s: None)
    c = g["stats"]["completion"]
    assert c["added_slide"] == 1 and c["added_external"] == 0


def test_both_evidence_fail_records_both_reasons(llm):
    def answer(user):
        return [JB(pair_no(user, "kouno", "otsu"), "kouno", "otsu", "提唱", 1, 1, "無い文", 1, "記事に無い文")]
    llm["answer"] = answer
    g = extraction.complete_relations(None, "m", VOCAB, graph(), PAGES, index=None, wiki=FakeWiki(WIKI_KOUNO),
                                      log=lambda s: None)
    c = g["stats"]["completion"]
    rec = {(r["a"], r["b"]): r for r in c["pairs"]}[("kouno", "otsu")]
    assert rec["result"] == "rejected" and rec["reason"] == "evidence が本文に無い / evidence が記事に無い"
    assert c["dropped"]["evidence が本文に無い"] == 1 and "evidence が記事に無い" not in c["dropped"]


def test_prompt_asks_for_wikipedia_evidence_separately(llm):
    extraction.complete_relations(None, "m", VOCAB, graph(), PAGES, index=None, wiki=FakeWiki(WIKI_KOUNO),
                                  log=lambda s: None)
    user = llm["users"][0]
    assert "資料に明示が無くても" in user and "external_found" in user and "wiki_evidence" in user
    props = extraction.COMPLETION_SCHEMA["properties"]["judgements"]["items"]["properties"]
    assert "origin" not in props and {"slide_found", "external_found"} <= set(props)


def test_evidence_must_name_both_ends(llm):
    """evidence が本文にあっても、片方の概念名しか無ければ足さない（#92）。"""
    pages = [dict(PAGES[0], text="甲野は乙説を唱えた。その説は広まった。丙も出てくる。"), *PAGES[1:]]

    def answer(user):
        return [J(pair_no(user, "kouno", "otsu"), "slide", "kouno", "otsu", "提唱", 1, 1, evidence="その説は広まった")]
    llm["answer"] = answer
    g = extraction.complete_relations(None, "m", VOCAB, graph(), pages, index=None, wiki=None, log=lambda s: None)
    c = g["stats"]["completion"]
    assert c["added_slide"] == 0 and c["dropped"]["evidence に両端が無い"] == 1


def test_external_evidence_must_name_both_ends(llm):
    wiki = FakeWiki({"甲野": {"title": "甲野", "url": "U", "extract": "甲野は哲学者。乙説の提唱者として知られる。彼は旅を好んだ。"}})

    def answer(user):
        return [J(pair_no(user, "kouno", "otsu"), "external", "kouno", "otsu", "提唱", wiki=1, evidence="彼は旅を好んだ")]
    llm["answer"] = answer
    g = extraction.complete_relations(None, "m", VOCAB, graph(), PAGES, index=None, wiki=wiki, log=lambda s: None)
    assert g["stats"]["completion"]["dropped"]["evidence に両端が無い"] == 1


def test_person_name_part_counts():
    kant = {"label": "イマヌエル・カント", "kind": "人物"}
    duty = {"label": "義務(デューティ)", "kind": "概念"}
    assert extraction.check_ends_in_evidence("カントは義務を重んじた", [kant, duty], {"人物"}) is None
    # 人物の旗が無ければ、名前の一部では通さない
    assert extraction.check_ends_in_evidence("カントは義務を重んじた", [kant, duty], set()) == "evidence に両端が無い"
    # 人物以外は括弧の前の全体が要る
    assert extraction.check_ends_in_evidence("カントは義を重んじた", [kant, duty], {"人物"}) == "evidence に両端が無い"


def test_prompt_has_direction_and_definition_rules(llm):
    extraction.complete_relations(None, "m", VOCAB, graph(), PAGES, index=None, wiki=None, log=lambda s: None)
    user = llm["users"][0]
    assert "例（「A → B」の A が from、B が to）と同じ向き" in user
    assert "定義の言い換え" in user and "（例: A → B）" in user
    assert "引用には二つの概念の名前を両方入れる" in user


def test_judgements_are_called_in_parallel_and_checked_in_pair_order(monkeypatch):
    """判定の呼び出しを並列にしても、答えは対の順に照合し、ログも対の順に出す（#107）。"""
    import threading
    import time

    monkeypatch.setattr(extraction, "COMPLETION_BATCH", 1)
    lock, state = threading.Lock(), {"active": 0, "max_active": 0, "calls": 0}

    def fake(client, *, model, system, user, schema, max_tokens=32000):
        with lock:
            state["active"] += 1
            state["calls"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            first = state["calls"] == 1
        time.sleep(0.05 if first else 0.01)      # 先に呼んだ対ほど遅く終わる
        with lock:
            state["active"] -= 1
        try:
            n = pair_no(user, "kouno", "otsu")
        except AssertionError:
            return {"judgements": []}
        return {"judgements": [J(n, "slide", "kouno", "otsu", "提唱", 1, 1, evidence="甲野は乙説を唱えた")]}

    monkeypatch.setattr(extraction, "call_json", fake)
    logs: list[str] = []
    g = extraction.complete_relations(None, "m", VOCAB, graph(), PAGES, index=None, wiki=None,
                                      log=logs.append, workers=4)
    c = g["stats"]["completion"]
    assert c["judged"] >= 2 and c["api_calls"] == c["judged"] == state["calls"]
    assert state["max_active"] >= 2
    assert [r["from"] for r in g["relations"] if r.get("added_by")] == ["kouno"]
    steps = [s for s in logs if s.startswith("関係の補完 ") and "追加" in s and "〜" in s]
    assert [int(s.split()[1].split("〜")[0]) for s in steps] == list(range(1, c["judged"] + 1))
