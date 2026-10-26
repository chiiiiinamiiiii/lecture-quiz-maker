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


KINDS2 = kinds(("人物", True), ("概念", False), ("学説・立場", False)) + [
    {"name": "文献・作品", "definition": "", "examples": ["a", "b", "c"], "cited_work": True, "person": False}]


def test_widen_person_targets_adds_all_content_kinds():
    """この回の取り込み（c20261024-204852-732458）と同じ形の語彙（#74）。"""
    v = {"kinds": KINDS2, "relations": [
        rel("提唱", ["人物"], ["学説・立場"]),
        rel("批判", ["人物"], ["学説・立場", "概念"]),       # 2 つあるので触らない
        rel("依拠", ["人物"], ["文献・作品"]),               # cited_work だけ → 中身の種類を足す
        rel("構成要素", ["概念"], ["概念"]),                 # from に人物が無いので触らない
        rel("影響", ["人物"], ["人物"]),                     # 人物だけ → 中身の種類を足し、人物は残す
        rel(OTHER_LABEL, [], []),
    ]}
    notes = V.widen_person_targets(v)
    to = {r["label"]: r["to_kinds"] for r in v["relations"]}
    assert to["提唱"] == ["学説・立場", "概念"]
    assert to["批判"] == ["学説・立場", "概念"]
    assert to["依拠"] == ["文献・作品", "概念", "学説・立場"]
    assert to["構成要素"] == ["概念"]
    assert to["影響"] == ["人物", "概念", "学説・立場"]
    assert to[OTHER_LABEL] == []
    assert len(notes) == 3 and "「提唱」の to_kinds に 概念 を足した" in notes[0]
    assert all("文献・作品" not in n for n in notes)       # cited_work は足さない
    assert V.find_narrow_person_targets(v) == []          # 広げた後は警告が出ない


def test_widen_person_targets_noop_without_person_flag():
    old = [{k: x for k, x in k.items() if k != "person"} for k in KINDS2]
    v = {"kinds": old, "relations": [rel("提唱", ["人物"], ["学説・立場"])]}
    assert V.widen_person_targets(v) == [] and v["relations"][0]["to_kinds"] == ["学説・立場"]


def test_derive_vocab_records_widening(monkeypatch):
    from quizmap.common import Lecture

    def fake(client, *, model, system, user, schema, max_tokens=32000):
        return {"course": "c", "kinds": KINDS2, "relations": [rel("提唱", ["人物"], ["学説・立場"])]}
    monkeypatch.setattr(V, "call_json", fake)
    lec = Lecture(number=1, path=None, pages=["甲野は乙説を唱えた。"])
    out = V.derive_vocab(None, "m", [lec], "c", log=lambda s: None)
    assert out["relations"][0]["to_kinds"] == ["学説・立場", "概念"]
    assert out["meta"]["widened_to_kinds"] and out["meta"]["kind_warnings"][0] == out["meta"]["widened_to_kinds"][0]
    assert out["meta"]["retried_for_kind_gaps"] is False                 # 作り直しの API は使わない


# ---------------------------------------------------------------- 端が出典の文献の種類だけのラベル（#141）

KINDS3 = kinds(("人物", True), ("概念", False), ("規範", False)) + [
    {"name": "文献", "definition": "", "examples": ["a", "b", "c"], "cited_work": True, "person": False}]


def test_find_cited_ends():
    v = {"kinds": KINDS3, "relations": [rel("具体例", ["概念"], ["文献"]), rel("出典", ["文献"], ["概念"]),
                                        rel("著す", ["人物"], ["文献", "概念"]), rel(OTHER_LABEL, [], [])]}
    got = V.find_cited_ends(v)
    assert len(got) == 2 and "「具体例」の to_kinds" in got[0] and "「出典」の from_kinds" in got[1]


def test_drop_cited_ends_removes_cited_kinds_and_widens_empty_side():
    v = {"kinds": KINDS3, "relations": [rel("具体例", ["概念"], ["文献"]), rel("著す", ["人物"], ["文献", "概念"]),
                                        rel("前提", ["概念"], ["規範"]), rel(OTHER_LABEL, [], [])]}
    notes = V.drop_cited_ends(v)
    by = {r["label"]: (r["from_kinds"], r["to_kinds"]) for r in v["relations"]}
    assert by["具体例"] == (["概念"], ["概念", "規範"])        # 本だけだった側は、人物・本以外の種類に広げる
    assert by["著す"] == (["人物"], ["概念"])
    assert by["前提"] == (["概念"], ["規範"])
    assert len(notes) == 3 and V.find_cited_ends(v) == []


def test_drop_cited_ends_noop_for_old_vocab():
    v = {"kinds": [{"name": "文献"}, {"name": "概念"}], "relations": [rel("出典", ["文献"], ["概念"])]}
    assert V.drop_cited_ends(v) == [] and v["relations"][0]["from_kinds"] == ["文献"]


def test_derive_vocab_retries_once_for_cited_only_ends(monkeypatch):
    from quizmap.common import Lecture
    calls = []

    def fake(client, *, model, system, user, schema, max_tokens=32000):
        calls.append(user)
        return {"course": "c", "kinds": KINDS3, "relations": [rel("具体例", ["概念"], ["文献"])]}
    monkeypatch.setattr(V, "call_json", fake)
    lec = Lecture(number=1, path=None, pages=["甲野は乙説を唱えた。"])
    out = V.derive_vocab(None, "m", [lec], "c", log=lambda s: None)
    assert len(calls) == 2 and "出典の文献の種類（文献）だけ" in calls[1]
    assert out["meta"]["retried_for_kind_gaps"] is True
    ex = next(r for r in out["relations"] if r["label"] == "具体例")
    assert ex["to_kinds"] == ["概念", "規範"] and out["meta"]["dropped_cited_ends"]   # 作り直しても残れば広げる


def test_prompt_says_cited_kinds_are_not_relation_ends():
    assert "関係ラベルの from_kinds・to_kinds に cited_work の種類を入れない" in V.USER_TEMPLATE
