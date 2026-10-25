"""quizmap.extraction.extract_relations（#101: 数ページずつ・前のかたまりと 1 ページ重ねる・そのページの概念だけ渡す）。

LLM は extraction.call_json を差し替える。API は呼ばない。
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest

from quizmap import extraction
from quizmap.common import OTHER_LABEL, Lecture

VOCAB = {
    "course": "テスト",
    "kinds": [{"name": "人物"}, {"name": "学説"}],
    "relations": [
        {"label": "提唱", "definition": "人物が学説を唱えた", "example": "A → B",
         "from_kinds": ["人物"], "to_kinds": ["学説"]},
        {"label": OTHER_LABEL, "definition": "", "example": ""},
    ],
}


def concept(cid, label, kind, *pages, lecture=1):
    return {"id": cid, "label": label, "kind": kind, "definition": "",
            "sources": [{"lecture": lecture, "page": p} for p in pages], "lectures": [lecture]}


class FakeLLM:
    """プロンプトのページ範囲（p.X〜p.Y）ごとに answers[(X, Y)] を返し、渡された概念 id を記録する。"""

    def __init__(self, answers):
        self.answers = answers
        self.seen = {}
        self.lock = threading.Lock()

    def __call__(self, client, *, model, system, user, schema, **kw):
        m = re.search(r"p\.(\d+)〜p\.(\d+)", user)
        key = (int(m.group(1)), int(m.group(2)))
        ids = re.findall(r"^- ([a-z_]+): ", user.split("関係ラベル")[0], flags=re.M)
        with self.lock:
            self.seen[key] = ids
        return {"relations": self.answers.get(key, [])}


def rel(f, t, page, evidence, label="提唱"):
    return {"from": f, "to": t, "label": label, "page": page, "evidence": evidence}


def run(monkeypatch, lectures, concepts, answers, size=2, overlap=1):
    fake = FakeLLM(answers)
    monkeypatch.setattr(extraction, "call_json", fake)
    info = {}
    logs = []
    relations, dropped, adjusted = extraction.extract_relations(
        None, "m", "テスト", VOCAB, lectures, {c["id"]: c for c in concepts}, workers=2, log=logs.append,
        pages_per_call=size, overlap=overlap, info=info)
    return relations, dropped, info, fake, logs


@pytest.mark.parametrize("n, size, overlap, want", [
    (5, 2, 1, None),
    (8, 4, 1, [(1, 4), (4, 8)]),
    (9, 4, 1, [(1, 3), (3, 6), (6, 9)]),
    (9, 4, 0, [(1, 3), (4, 6), (7, 9)]),
    (3, 4, 1, [(1, 3)]),
    (6, 0, 1, [(1, 6)]),
])
def test_relation_chunks(n, size, overlap, want):
    got = extraction.relation_chunks(n, size, overlap)
    if want is not None:
        assert got == want
    assert got[0][0] == 1 and got[-1][1] == n


def test_only_concepts_on_chunk_pages_are_passed(monkeypatch):
    lec = Lecture(1, Path("1.pdf"), ["甲は乙説を唱えた", "x", "丙は丁説を唱えた", "y"])
    concepts = [concept("kou", "甲", "人物", 1), concept("otsu", "乙説", "学説", 1),
                concept("hei", "丙", "人物", 3), concept("tei", "丁説", "学説", 3)]
    _, _, info, fake, _ = run(monkeypatch, [lec], concepts, {}, size=2, overlap=1)
    assert sorted(fake.seen) == [(1, 2), (2, 4)]
    assert sorted(fake.seen[(1, 2)]) == ["kou", "otsu"]
    assert sorted(fake.seen[(2, 4)]) == ["hei", "tei"]
    assert info["calls"] == 2


def test_chunk_with_fewer_than_two_concepts_is_not_called(monkeypatch):
    lec = Lecture(1, Path("1.pdf"), ["甲は乙説を唱えた", "x", "y", "丙"])
    concepts = [concept("kou", "甲", "人物", 1), concept("otsu", "乙説", "学説", 1), concept("hei", "丙", "人物", 4)]
    _, _, info, fake, logs = run(monkeypatch, [lec], concepts, {}, size=2, overlap=0)
    assert sorted(fake.seen) == [(1, 2)]
    assert info["skipped_chunks"] == 1
    assert sum(l.startswith("関係 第1回") for l in logs) == 1      # 進み具合の行は回ごとに 1 行


def test_relation_on_overlap_page_is_kept_once(monkeypatch):
    lec = Lecture(1, Path("1.pdf"), ["x", "甲は乙説を唱えた", "z"])
    concepts = [concept("kou", "甲", "人物", 2), concept("otsu", "乙説", "学説", 2)]
    answers = {(1, 2): [rel("kou", "otsu", 2, "甲は乙説を唱えた")], (2, 3): [rel("kou", "otsu", 2, "甲は乙説を唱えた")]}
    relations, dropped, _, _, _ = run(monkeypatch, [lec], concepts, answers, size=2, overlap=1)
    assert [(r["from"], r["to"], r["source"]) for r in relations] == [("kou", "otsu", {"lecture": 1, "page": 2})]
    assert dropped.get("重複") == 1


def test_page_outside_chunk_is_dropped(monkeypatch):
    lec = Lecture(1, Path("1.pdf"), ["甲は乙説を唱えた", "x", "甲と乙説"])
    concepts = [concept("kou", "甲", "人物", 1, 3), concept("otsu", "乙説", "学説", 1, 3)]
    answers = {(1, 2): [rel("kou", "otsu", 3, "甲と乙説")]}
    relations, dropped, _, _, _ = run(monkeypatch, [lec], concepts, answers, size=2, overlap=0)
    assert relations == [] and dropped["ページ番号が範囲外"] == 1


def test_lectures_are_separate(monkeypatch):
    l1 = Lecture(1, Path("1.pdf"), ["甲は乙説を唱えた"])
    l2 = Lecture(2, Path("2.pdf"), ["丙"])
    concepts = [concept("kou", "甲", "人物", 1), concept("otsu", "乙説", "学説", 1),
                concept("hei", "丙", "人物", 1, lecture=2)]
    _, _, info, fake, logs = run(monkeypatch, [l1, l2], concepts, {}, size=2)
    assert fake.seen == {(1, 1): ["kou", "otsu"]}
    assert sorted(l for l in logs if l.startswith("関係 第")) == ["関係 第1回: 候補 0", "関係 第2回: 候補 0（概念が 2 つ以上あるかたまりが無い）"]


def test_concept_named_on_page_is_passed_even_without_source(monkeypatch):
    lec = Lecture(1, Path("1.pdf"), ["甲は乙説を唱えた", "x", "甲野花子は乙説を批判した", "y"])
    concepts = [concept("kou", "甲", "人物", 1), concept("otsu", "乙説", "学説", 1),
                concept("hanako", "甲野・花子", "人物", 1)]
    vocab_person = dict(VOCAB, kinds=[{"name": "人物", "person": True}, {"name": "学説"}])
    fake = FakeLLM({})
    monkeypatch.setattr(extraction, "call_json", fake)
    extraction.extract_relations(None, "m", "テスト", vocab_person, [lec], {c["id"]: c for c in concepts}, workers=1,
                                 log=lambda m: None, pages_per_call=2, overlap=0)
    # p.3-4: 乙説 は名前で、甲野・花子 は人物の名前の部分（甲野）で入る。1 字の「甲」は名前では入れない
    assert sorted(fake.seen[(3, 4)]) == ["hanako", "otsu"]
