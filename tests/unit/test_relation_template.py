"""関係ラベルの文の型（template）と正答になる端（answer_end）（#155・#156）。API は呼ばない。"""
from __future__ import annotations

import pytest

from quizmap import extraction, quiz
from quizmap import vocab as V
from quizmap.common import OTHER_LABEL

GROUND = {"label": "根拠", "definition": "from が理由、to が判断", "question_style": "A（to）の根拠（from）を問う",
          "example": "自律 → 尊厳", "template": "{from}は{to}の根拠である", "answer_end": "from",
          "from_kinds": ["概念"], "to_kinds": ["概念"]}


def test_relation_sentence():
    assert V.relation_sentence(GROUND, "自律", "尊厳") == "自律は尊厳の根拠である"
    assert V.relation_sentence({"template": "{from} is a part of {to}"}, "x", "y") == "x is a part of y"
    assert V.relation_sentence({"template": "{from}と{from}"}, "x", "y") is None
    assert V.relation_sentence({"label": "旧"}, "x", "y") is None
    assert V.relation_sentence(None, "x", "y") is None


def test_template_gaps():
    ok = {"kinds": [], "relations": [GROUND, {"label": OTHER_LABEL}]}
    assert V.find_template_gaps(ok) == []
    bad = {"kinds": [], "relations": [{**GROUND, "template": "根拠である", "answer_end": "x"}]}
    got = V.find_template_gaps(bad)
    assert len(got) == 2 and "「{from}」と「{to}」" in got[0]
    assert V.find_template_gaps({"kinds": [], "relations": [{"label": "旧"}]}) == []       # 型の無い古い形


def test_vocab_schema_and_prompt():
    items = V.SCHEMA["properties"]["relations"]["items"]
    assert {"template", "answer_end"} <= set(items["required"])
    assert "「{from}」「{to}」を必ず 1 つずつ含み" in V.USER_TEMPLATE.replace("{{", "{").replace("}}", "}")
    assert "ふつうの読み方の向きにする" in V.USER_TEMPLATE


def test_label_line_has_template():
    line = extraction._label_line(GROUND, {})
    assert "【文の型: 「{from}は{to}の根拠である」】" in line
    old = {k: v for k, v in GROUND.items() if k not in ("template", "answer_end")}
    assert "文の型" not in extraction._label_line(old, {})


def test_structure_rules_mention_template_and_quantities():
    for prompt in (extraction.EXTRACT_USER, extraction.COMPLETION_USER, extraction.CONNECT_USER):
        body = prompt.replace("{{", "{").replace("}}", "}")
        assert "【文の型】があるときは" in body and "量どうしを、一種・下位概念の関係にしない" in body


CONCEPTS = {"jiritsu": {"id": "jiritsu", "label": "自律"}, "songen": {"id": "songen", "label": "尊厳"}}
INNER = [{"from": "jiritsu", "to": "songen", "label": "根拠", "source": {"lecture": 1, "page": 2}}]


def test_materials_use_sentence_and_answer():
    text = quiz.materials_text(CONCEPTS, [], INNER, {}, [], {}, [GROUND])
    assert "- 「自律は尊厳の根拠である」（根拠。from は「自律」、to は「尊厳」。この関係を問うときは「尊厳」を示し、正答は「自律」）" in text
    assert "文の型「{from}は{to}の根拠である」／正答になる端: from" in text


def q(answer_text, used=("自律", "尊厳", "根拠")):
    return {"id": "q1", "aspect": "relation", "answer_text": answer_text,
            "relation_used": dict(zip(("from", "to", "label"), used))}


@pytest.mark.parametrize("question, dropped", [
    (q("自律"), False),                                   # 正答は from（自律）
    (q("尊厳"), True),                                    # 正答がもう一方の端
    (q("自律", used=("尊厳", "自律", "根拠")), True),      # relation_used が逆向き
    (q("自由"), False),                                   # どちらの端でもない（ほかの検証に任せる）
    ({**q("尊厳"), "aspect": "definition"}, False),
])
def test_relation_direction_problem(question, dropped):
    got = quiz.relation_direction_problem(question, INNER, CONCEPTS, [GROUND])
    assert (got == quiz.RELATION_DIRECTION_REASON) is dropped


def test_no_answer_end_keeps_question():
    style = {k: v for k, v in GROUND.items() if k != "answer_end"}
    assert quiz.relation_direction_problem(q("尊厳"), INNER, CONCEPTS, [style]) is None
