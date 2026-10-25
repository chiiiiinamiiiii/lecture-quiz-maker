"""quizmap.extraction.extract_concepts（#101: 数ページずつ呼ぶ・名前が本文に無い概念を落とす）。

LLM は extraction.call_json を差し替える。API は呼ばない。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from quizmap import extraction
from quizmap.common import OTHER_LABEL, Lecture

VOCAB = {
    "course": "テスト",
    "language": "ja",
    "kinds": [
        {"name": "人物", "definition": "人", "examples": ["甲野"], "person": True},
        {"name": "学説", "definition": "考え方", "examples": ["乙説"]},
    ],
    "relations": [{"label": OTHER_LABEL, "definition": "", "example": ""}],
}


def lec(number, *pages):
    return Lecture(number=number, path=Path(f"第{number}回.pdf"), pages=list(pages))


def c(cid, label, kind, *pages):
    return {"id": cid, "label": label, "kind": kind, "definition": "説明", "pages": list(pages)}


class FakeLLM:
    """呼ばれるたびに、渡されたページ範囲（p.X〜p.Y）を記録し、answers の順に返す。"""

    def __init__(self, answers):
        self.answers = list(answers)
        self.users = []

    def __call__(self, client, *, model, system, user, schema, **kw):
        self.users.append(user)
        return {"concepts": self.answers.pop(0)}


def run(monkeypatch, lectures, answers, pages_per_call=2):
    fake = FakeLLM(answers)
    monkeypatch.setattr(extraction, "call_json", fake)
    concepts, unmatched, info = extraction.extract_concepts(
        None, "m", "テスト", VOCAB, lectures, log=lambda m: None, pages_per_call=pages_per_call)
    return concepts, unmatched, info, fake


@pytest.mark.parametrize("n, size, want", [
    (5, 4, [(1, 3), (4, 5)]),
    (9, 4, [(1, 3), (4, 6), (7, 9)]),
    (8, 4, [(1, 4), (5, 8)]),
    (3, 4, [(1, 3)]),
    (18, 4, None),
    (6, 0, [(1, 6)]),
    (0, 4, []),
])
def test_page_chunks(n, size, want):
    got = extraction.page_chunks(n, size)
    if want is not None:
        assert got == want
    # どの場合も 1〜n を漏れなく重なりなく覆い、各かたまりは size 以下
    covered = [p for a, b in got for p in range(a, b + 1)]
    assert covered == list(range(1, n + 1))
    if size > 0:
        assert all(b - a + 1 <= size for a, b in got)


def test_calls_per_chunk_and_only_chunk_pages_in_prompt(monkeypatch):
    lectures = [lec(1, "甲野の話", "乙説の話", "丙説の話")]
    _, _, info, fake = run(monkeypatch, lectures, [[c("kouno", "甲野", "人物", 1)], [c("hei", "丙説", "学説", 3)]])
    assert info["calls"] == 2
    assert "p.1〜p.2" in fake.users[0] and "[第1回 p.3]" not in fake.users[0]
    assert "p.3〜p.3" in fake.users[1] and "[第1回 p.1]" not in fake.users[1]


def test_known_concepts_passed_and_relisted_concept_merged(monkeypatch):
    lectures = [lec(1, "甲野の話", "乙説"), lec(2, "甲野ふたたび", "丁説")]
    concepts, _, info, fake = run(monkeypatch, lectures, [
        [c("kouno", "甲野", "人物", 1), c("otsu", "乙説", "学説", 2)],
        [c("kouno", "甲野", "人物", 1), c("tei", "丁説", "学説", 2)],
    ])
    assert "kouno: 甲野" in fake.users[1]           # 既出の一覧を渡す
    assert "既出かどうかにかかわらず" in fake.users[1]
    assert set(concepts) == {"kouno", "otsu", "tei"}
    assert concepts["kouno"]["sources"] == [{"lecture": 1, "page": 1}, {"lecture": 2, "page": 1}]
    assert concepts["kouno"]["lectures"] == [1, 2]
    assert info["new_by_lecture"] == {1: 2, 2: 1}
    assert info["listed_by_lecture"] == {1: 2, 2: 2}


def test_label_not_on_page_is_dropped_and_recorded(monkeypatch):
    lectures = [lec(1, "甲野の話", "乙説の話")]
    concepts, _, info, _ = run(monkeypatch, lectures, [[
        c("kouno", "甲野", "人物", 1),
        c("muga", "無我説", "学説", 2),          # 資料に無い名前
    ]])
    assert set(concepts) == {"kouno"}
    assert info["label_missing_count"] == 1
    assert info["label_missing_examples"][0]["label"] == "無我説"
    assert info["label_missing_examples"][0]["lecture"] == 1


def test_label_compared_after_nfkc_and_without_spaces_and_before_paren(monkeypatch):
    lectures = [lec(1, "ＡＢＣ 理論 と 理 の話")]
    concepts, _, info, _ = run(monkeypatch, lectures, [[
        c("abc", "ABC理論", "学説", 1),
        c("ri", "理（ロゴス）", "学説", 1),
    ]])
    assert set(concepts) == {"abc", "ri"}
    assert info["label_missing_count"] == 0


def test_person_name_part_is_enough(monkeypatch):
    lectures = [lec(1, "カントは義務を説いた"), lec(2, "ミルの話")]
    concepts, _, info, _ = run(monkeypatch, lectures, [
        [c("kant", "イマヌエル・カント", "人物", 1)],
        [c("mill", "ジョン=スチュアート=ミル", "人物", 1), c("smith", "ミル説", "学説", 1)],
    ])
    assert "kant" in concepts and "mill" in concepts
    assert "smith" not in concepts                    # 人物でない種類は区切らない（ミル説 は本文に無い）
    assert info["label_missing_count"] == 1


def test_claimed_page_without_name_moves_to_page_with_name(monkeypatch):
    lectures = [lec(1, "前置き", "乙説の話")]
    concepts, _, info, _ = run(monkeypatch, lectures, [[c("otsu", "乙説", "学説", 1)]])
    assert concepts["otsu"]["sources"] == [{"lecture": 1, "page": 2}]
    assert info["pages_corrected"] == 1


def test_mention_without_name_does_not_drop_known_concept(monkeypatch):
    lectures = [lec(1, "乙説の話"), lec(2, "それについての続き")]
    concepts, _, info, _ = run(monkeypatch, lectures, [
        [c("otsu", "乙説", "学説", 1)],
        [c("otsu", "乙説", "学説", 1)],              # 第2回には名前が無い
    ])
    assert concepts["otsu"]["sources"] == [{"lecture": 1, "page": 1}]
    assert concepts["otsu"]["lectures"] == [1]
    assert info["label_missing_count"] == 0
    assert info["label_missing_mentions"] == 1


def test_pages_out_of_chunk_are_ignored(monkeypatch):
    lectures = [lec(1, "甲野", "x", "甲野も")]
    concepts, _, _, _ = run(monkeypatch, lectures, [[c("kouno", "甲野", "人物", 1, 3)], []])
    assert concepts["kouno"]["sources"] == [{"lecture": 1, "page": 1}]


def test_zero_pages_per_call_means_one_call_per_lecture(monkeypatch):
    lectures = [lec(1, "甲野", "乙説", "丙説")]
    _, _, info, fake = run(monkeypatch, lectures, [[c("kouno", "甲野", "人物", 1)]], pages_per_call=0)
    assert info["calls"] == 1 and "p.1〜p.3" in fake.users[0]
