"""quizmap.vocab の種類の検証（#74）。API は呼ばない。"""
from __future__ import annotations

from quizmap import vocab as V
from quizmap.common import OTHER_LABEL


def kinds(*specs):
    return [{"name": n, "definition": "", "examples": ["a", "b", "c"], "cited_work": False, "person": p}
            for n, p in specs]


def rel(label, f, t):
    return {"label": label, "definition": "", "question_style": "", "example": "", "from_kinds": f, "to_kinds": t}


KINDS = kinds(("人物", True), ("概念", False), ("立場", False), ("学説", False))


def test_narrow_target_of_person_label_is_warned():
    v = {"kinds": KINDS, "relations": [rel("提唱", ["人物"], ["立場"]), rel("批判", ["人物"], ["人物", "概念"]),
                                       rel(OTHER_LABEL, [], [])]}
    w = V.find_narrow_person_targets(v)
    assert len(w) == 2
    assert "「提唱」" in w[0] and "1 個（立場）" in w[0]
    assert "「批判」" in w[1] and "1 個（概念）" in w[1]
    assert all(x in V.find_kind_warnings(v) for x in w)


def test_wide_target_or_non_person_label_is_not_warned():
    v = {"kinds": KINDS, "relations": [rel("提唱", ["人物"], ["概念", "立場", "学説"]), rel("前提", ["概念"], ["概念"])]}
    assert V.find_narrow_person_targets(v) == []


def test_old_vocab_without_person_flag_is_silent():
    old = [{k: v for k, v in x.items() if k != "person"} for x in KINDS]
    v = {"kinds": old, "relations": [rel("提唱", ["人物"], ["立場"])]}
    assert V.find_narrow_person_targets(v) == []


def test_prompt_and_schema():
    assert "人物・思想家を主語とするラベル" in V.USER_TEMPLATE
    assert "関係の両端の種類をすべて含めること" in V.USER_TEMPLATE
    item = V.SCHEMA["properties"]["kinds"]["items"]
    assert "person" in item["required"]
