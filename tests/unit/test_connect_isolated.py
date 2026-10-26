"""quizmap.extraction.connect_isolated（つながらない概念をつなぐ、#127）。

LLM は extraction.call_json を差し替え、SlideIndex は本物をページのベクトルから作る。API は呼ばない。
"""
from __future__ import annotations

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
    assert rels["bo"]["to"] in ("hei", "tei") and rels["bo"]["via"] == "same_page"     # 同じページの、関係の多い概念
    assert rels["bo"]["source"] == {"lecture": 1, "page": 2}
    assert rels["ki"]["via"] == "adjacent_page" and rels["ki"]["source"] == {"lecture": 1, "page": 3}
    assert rels["kou"]["via"] == "same_lecture" and rels["kou"]["to"] == "ki"           # 同じ回で、ページの近い順
    assert rels["shin"]["via"] == "vector" and rels["shin"]["to"] in ("kouno", "otsu")  # 別の回だけなので索引で近いページ
    for r in rels.values():
        assert r["label"] == extraction.RELATED_LABEL and r["origin"] == "cooccurrence" and "evidence" not in r
    st = g["stats"]["connect"]
    assert st["isolated_before"] == 4 and st["isolated_after"] == 0
    assert st["stage2"] == {"added": 4, "by_via": {"same_page": 1, "adjacent_page": 1, "same_lecture": 1, "vector": 1}}
    assert g["stats"]["isolated_concepts"] == 0 and g["stats"]["relations"] == 6


def test_stage2_without_index_and_without_pages_falls_back():
    gr = graph()
    gr["concepts"].append(concept("nopage", "癸説", "学説"))
    g = extraction.connect_isolated(None, "m", VOCAB, gr, PAGES, None, None, log=lambda *_: None)
    rels = by_from(g)
    assert rels["shin"]["via"] == "fallback" or rels["shin"]["via"] == "same_lecture"
    assert rels["nopage"]["via"] == "fallback"
    assert g["stats"]["connect"]["isolated_after"] == 0


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
    assert [(o, v) for o, v, _ in out] == [("hei", "adjacent_page"), ("tei", "adjacent_page"), ("bo", "adjacent_page"),
                                           ("kouno", "same_lecture"), ("otsu", "same_lecture"),   # 同じ距離なら関係の多い順
                                           ("kou", "same_lecture"), ("shin", "vector")]
