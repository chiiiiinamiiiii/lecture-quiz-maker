"""quizmap.extraction.connect_isolated（つながらない概念をつなぐ、#127）。

LLM は extraction.call_json を差し替え、SlideIndex は本物をページのベクトルから作る。API は呼ばない。
"""
from __future__ import annotations

import collections

import pytest

from quizmap import extraction
from quizmap.common import OTHER_LABEL

VOCAB = {
    "course": "テスト",
    "kinds": [{"name": "人物", "person": True, "cited_work": False}, {"name": "学説", "person": False, "cited_work": False}],
    "relations": [
        {"label": "提唱", "definition": "人物が学説を唱えた", "example": "A → B",
         "from_kinds": ["人物"], "to_kinds": ["学説"]},
        {"label": OTHER_LABEL, "definition": "", "example": ""},
    ],
}

PAGES = [
    {"lecture": 1, "page": 1, "text": "甲野は乙説を唱えた。"},
    {"lecture": 1, "page": 2, "text": "丙は丁説を唱えた。戊説もある。"},
    {"lecture": 1, "page": 3, "text": "己説の話。"},
    {"lecture": 1, "page": 5, "text": "庚説の話。"},
    {"lecture": 2, "page": 1, "text": "辛説の話。"},
]


def concept(cid, label, kind, *pages):
    return {"id": cid, "label": label, "kind": kind, "definition": "説明",
            "sources": [{"lecture": l, "page": p} for l, p in pages], "lectures": sorted({l for l, _ in pages})}


def graph():
    return {
        "concepts": [
            concept("kouno", "甲野", "人物", (1, 1)),
            concept("otsu", "乙説", "学説", (1, 1)),
            concept("hei", "丙", "人物", (1, 2)),
            concept("tei", "丁説", "学説", (1, 2)),
            concept("bo", "戊説", "学説", (1, 2)),
            concept("ki", "己説", "学説", (1, 3)),
            concept("kou", "庚説", "学説", (1, 5)),
            concept("shin", "辛説", "学説", (2, 1)),
        ],
        "relations": [{"from": "kouno", "to": "otsu", "label": "提唱", "source": {"lecture": 1, "page": 1}},
                      {"from": "hei", "to": "tei", "label": "提唱", "source": {"lecture": 1, "page": 2}}],
        "stats": {"relations": 2},
    }


def page_index(vectors):
    import numpy as np

    from quizmap import rag
    m = np.array([vectors[(p["lecture"], p["page"])] for p in PAGES], dtype=np.float32)
    return rag.SlideIndex(PAGES, rag._normalize(m))


VEC = {(1, 1): [1, 0], (1, 2): [0.9, 0.1], (1, 3): [0, 1], (1, 5): [0.5, 0.5], (2, 1): [0.95, 0.05]}


def by_from(g):
    return {r["from"]: r for r in g["relations"] if r.get("added_by") == "connect"}


def test_stage2_connects_every_isolated_concept_by_position():
    g = extraction.connect_isolated(None, "m", VOCAB, graph(), PAGES, page_index(VEC), None, log=lambda *_: None)
    rels = by_from(g)
    assert set(rels) == {"bo", "ki", "kou", "shin"}
    assert rels["bo"]["to"] in ("hei", "tei") and rels["bo"]["via"] == "same_page"     # 同じページの概念
    assert rels["bo"]["source"] == {"lecture": 1, "page": 2}
    assert rels["ki"]["via"] == "adjacent_page" and rels["ki"]["source"] == {"lecture": 1, "page": 3}
    assert rels["kou"]["via"] == "same_lecture" and rels["kou"]["to"] == "tei"          # 同じ回で、内容の近い順
    assert rels["shin"]["via"] == "vector" and rels["shin"]["to"] in ("kouno", "otsu")  # 別の回だけなので索引で近いページ
    for r in rels.values():
        assert r["label"] == extraction.RELATED_LABEL and r["origin"] == "cooccurrence" and "evidence" not in r
    st = g["stats"]["connect"]
    assert st["isolated_before"] == 4 and st["isolated_after"] == 0
    assert st["stage2"] == {"added": 4, "by_via": {"same_page": 1, "adjacent_page": 1, "same_lecture": 1, "vector": 1},
                            "max_received": 1, "partners": 4, "similarity": True}
    assert g["stats"]["isolated_concepts"] == 0 and g["stats"]["relations"] == 6


def test_stage2_without_index_and_without_pages_falls_back():
    gr = graph()
    gr["concepts"].append(concept("nopage", "癸説", "学説"))
    g = extraction.connect_isolated(None, "m", VOCAB, gr, PAGES, None, None, log=lambda *_: None)
    rels = [r for r in g["relations"] if r.get("added_by") == "connect"]
    assert {r["via"] for r in rels if "shin" in (r["from"], r["to"])} == {"fallback"}   # 別の回だけ。索引が無いので fallback
    assert any("nopage" in (r["from"], r["to"]) for r in rels)
    assert g["stats"]["connect"]["isolated_after"] == 0
    assert g["stats"]["connect"]["stage2"]["similarity"] is False


def test_nothing_to_do_when_all_linked():
    gr = graph()
    gr["concepts"] = gr["concepts"][:4]
    g = extraction.connect_isolated(None, "m", VOCAB, gr, PAGES, None, None, log=lambda *_: None)
    assert g["relations"] == [dict(r, origin="slide") for r in gr["relations"]]
    assert g["stats"]["connect"]["isolated_before"] == 0 and g["stats"]["connect"]["stage2"]["added"] == 0


@pytest.fixture
def llm(monkeypatch):
    state = {"answer": lambda user: [], "users": []}

    def fake(client, *, model, system, user, schema, max_tokens=32000):
        state["users"].append(user)
        assert schema is extraction.CONNECT_SCHEMA
        return {"answers": state["answer"](user)}

    monkeypatch.setattr(extraction, "call_json", fake)
    return state


def targets(user):
    """プロンプトの「つなぐ概念」の行から、id → 番号。"""
    lines = [l for l in user.splitlines() if l[:1].isdigit() and "（" in l]
    return {l.split("（")[0].split(". ")[1]: int(l.split(".")[0]) for l in lines}


def A(target, partner, f, t, label="提唱", lecture=0, page=0, evidence="", slide=True):
    return {"target": target, "partner": partner, "from": f, "to": t, "label": label, "slide_found": slide,
            "lecture": lecture, "page": page, "slide_evidence": evidence, "external_found": False, "wiki": 0,
            "wiki_evidence": ""}


def test_stage1_adds_verified_relation_and_stage2_takes_the_rest(llm):
    def answer(user):
        num = targets(user)
        return [A(num["bo"], "hei", "hei", "bo", lecture=1, page=2, evidence="丙は丁説を唱えた"),   # 戊説が evidence に無い
                A(num["bo"], "hei", "hei", "bo")]                                                # 同じ番号の 2 つ目は数えない
    llm["answer"] = answer
    g = extraction.connect_isolated(object(), "m", VOCAB, graph(), PAGES, page_index(VEC), None, log=lambda *_: None)
    st = g["stats"]["connect"]
    assert st["stage1"]["targets"] == 4 and st["stage1"]["api_calls"] == 1
    assert st["stage1"]["added_slide"] == 0
    assert st["isolated_after"] == 0 and st["stage2"]["added"] == 4


def test_stage1_relation_passes_the_same_checks_as_completion(llm):
    pages = [dict(p) for p in PAGES]
    pages[1]["text"] = "丙は戊説を唱えた。丁説もある。"
    def answer(user):
        num = targets(user)
        if "bo" not in num:
            return []
        return [A(num["bo"], "hei", "bo", "hei", lecture=1, page=2, evidence="丙は戊説を唱えた"),   # 向きは直す
                A(num["ki"], "nobody", "ki", "nobody", lecture=1, page=3, evidence="己説の話")]   # 候補に無い
    llm["answer"] = answer
    g = extraction.connect_isolated(object(), "m", VOCAB, graph(), pages, page_index(VEC), None, log=lambda *_: None,
                                    batch_size=2)
    rels = [r for r in g["relations"] if r.get("added_by") == "connect"]
    slide = [r for r in rels if r["origin"] == "slide"]
    assert slide == [{"from": "hei", "to": "bo", "label": "提唱", "source": {"lecture": 1, "page": 2},
                      "origin": "slide", "evidence": "丙は戊説を唱えた", "added_by": "connect"}]
    st = g["stats"]["connect"]
    assert st["stage1"]["api_calls"] == 2 and st["stage1"]["added_slide"] == 1
    assert st["stage1"]["dropped"]["候補に無い相手"] == 1
    assert st["stage1"]["adjusted"] == {"向きを直した": 1}
    assert st["isolated_after"] == 0 and st["stage2"]["added"] == 3
    prompt = llm["users"][0]
    assert "候補:" in prompt and "必ず 1 件答える" in prompt and "[第1回 p2]" in prompt


def test_candidates_order():
    gr = graph()
    concepts = {c["id"]: c for c in gr["concepts"]}
    on_page = {}
    for c in gr["concepts"]:
        for s in c["sources"]:
            on_page.setdefault((s["lecture"], s["page"]), []).append(c["id"])
    deg = extraction._degree(gr["relations"])
    out = extraction.connect_candidates("ki", concepts, on_page, deg, [{"lecture": 2, "page": 1}])
    # 索引が無いとき: 段の中は関係の少ない順（同じ回はページの近い順を先に）
    assert [(o, v) for o, v, _ in out] == [("bo", "adjacent_page"), ("hei", "adjacent_page"), ("tei", "adjacent_page"),
                                           ("kou", "same_lecture"), ("kouno", "same_lecture"),
                                           ("otsu", "same_lecture"), ("shin", "vector")]
    # 索引があるとき: 段の中は内容の近い順
    sim = extraction.similarities("ki", extraction.concept_vectors(page_index(VEC), concepts))
    out = extraction.connect_candidates("ki", concepts, on_page, deg, [], sim)
    assert [o for o, v, _ in out if v == "same_lecture"] == ["kou", "kouno", "otsu"]


def test_each_concept_receives_at_most_three_related():
    """同じページに関係の多い概念が 1 つだけあっても、「関連」は 3 本までで、残りは次の候補に回す（星形を避ける）。"""
    pages = [{"lecture": 1, "page": 1, "text": "x"}]
    gr = {"concepts": [concept("hub", "中心", "学説", (1, 1)), concept("o", "相手", "学説", (1, 1))]
          + [concept(f"c{i}", f"概念{i}", "学説", (1, 1)) for i in range(8)],
          "relations": [{"from": "hub", "to": "o", "label": "提唱", "source": {"lecture": 1, "page": 1}}]}
    g = extraction.connect_isolated(None, "m", VOCAB, gr, pages, None, None, log=lambda *_: None)
    got = collections.Counter(r["to"] for r in g["relations"] if r.get("added_by") == "connect")
    assert max(got.values()) <= 3 and g["stats"]["connect"]["isolated_after"] == 0
    st = g["stats"]["connect"]["stage2"]
    assert st["max_received"] <= 3 and st["partners"] == len(got)


def test_over_cap_only_when_no_one_else():
    pages = [{"lecture": 1, "page": 1, "text": "x"}]
    gr = {"concepts": [concept("a", "甲", "学説", (1, 1)), concept("b", "乙", "学説", (1, 1)),
                       concept("c", "丙", "学説", (1, 1))], "relations": []}
    g = extraction.connect_isolated(None, "m", VOCAB, gr, pages, None, None, log=lambda *_: None, max_received=0)
    assert g["stats"]["connect"]["isolated_after"] == 0
    assert {r["via"] for r in g["relations"]} == {"over_cap"}


def test_link_groups():
    gr = graph()
    rels = gr["relations"] + [{"from": "otsu", "to": "bo", "label": "提唱"}]
    concepts = {c["id"]: c for c in gr["concepts"]}
    assert extraction.link_groups(concepts, rels) == {"kouno": 0, "otsu": 0, "bo": 0, "hei": 1, "tei": 1,
                                                      "ki": 2, "kou": 2, "shin": 2}


def test_prefers_main_component_over_isolated_in_the_same_tier():
    """同じページに、内容のより近いつながらない概念がいても、最大の連結成分の概念を先に選ぶ（島を作らない）。"""
    pages = [{"lecture": 1, "page": 1, "text": "x"}, {"lecture": 1, "page": 2, "text": "y"},
             {"lecture": 1, "page": 3, "text": "z"}]
    gr = {"concepts": [concept("a", "甲", "学説", (1, 1)), concept("b", "乙", "学説", (1, 1)),
                       concept("c", "丙", "学説", (1, 1), (1, 2)), concept("iso1", "丁", "学説", (1, 2), (1, 3)),
                       concept("iso2", "戊", "学説", (1, 2), (1, 3))],
          "relations": [{"from": "a", "to": "b", "label": "提唱", "source": {"lecture": 1, "page": 1}}]}
    import numpy as np

    from quizmap import rag
    index = rag.SlideIndex(pages, rag._normalize(np.array([[1, 0], [0.7, 0.7], [0, 1]], dtype=np.float32)))
    g = extraction.connect_isolated(None, "m", VOCAB, gr, pages, index, None, log=lambda *_: None)
    rels = {r["from"]: r["to"] for r in g["relations"] if r.get("added_by") == "connect"}
    assert rels["c"] in ("a", "b")                  # 前後のページではなく同じページの、最大の連結成分の概念
    assert rels["iso1"] == "c"                      # iso2 のほうが内容は近いが、つながっている c を先に
    assert rels["iso2"] in ("c", "iso1")            # iso1 はもう最大の連結成分に入っている
    concepts = {c["id"]: c for c in g["concepts"]}
    assert set(extraction.link_groups(concepts, g["relations"]).values()) == {0}   # 島が残らない
