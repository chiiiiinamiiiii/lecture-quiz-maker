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
    """概念の抽出の呼び出しには answers を順に、表記の聞き直しには surfaces を順に返す。"""

    def __init__(self, answers, surfaces=()):
        self.answers = list(answers)
        self.surfaces = list(surfaces)
        self.users, self.asks = [], []

    def __call__(self, client, *, model, system, user, schema, **kw):
        if schema is extraction.SURFACE_SCHEMA:
            self.asks.append(user)
            return {"answers": self.surfaces.pop(0)}
        self.users.append(user)
        return {"concepts": self.answers.pop(0)}


def run(monkeypatch, lectures, answers, pages_per_call=2, surfaces=()):
    fake = FakeLLM(answers, surfaces)
    monkeypatch.setattr(extraction, "call_json", fake)
    concepts, unmatched, info = extraction.extract_concepts(
        None, "m", "テスト", VOCAB, lectures, log=lambda m: None, pages_per_call=pages_per_call)
    return concepts, unmatched, info, fake


def labels(concepts):
    return sorted(c["label"] for c in concepts.values())


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


def test_label_written_as_on_page_needs_no_fix(monkeypatch):
    lectures = [lec(1, "ＡＢＣ 理論 の話")]
    concepts, _, info, fake = run(monkeypatch, lectures, [[c("abc", "ABC理論", "学説", 1)]])
    assert labels(concepts) == ["ABC 理論"]        # 本文の文字列（空白の入り方も本文どおり）
    assert info["label_fixes"] == [] and fake.asks == []


@pytest.mark.parametrize("page, label, kind, want, how", [
    ("ジョン・ロールズの正義論", "ジョンロールズ", "人物", "ジョン・ロールズ", "表記ゆれ"),
    ("「善意志」について", "善 意志", "学説", "善意志", "そのまま"),
    ("理(ロゴス)を伴う活動", "理（ロゴス）", "学説", "理(ロゴス)", "そのまま"),
    ("理 ロゴス を伴う", "理（ロゴス）", "学説", "理 ロゴス", "表記ゆれ"),
    ("取扱いの原則", "取り扱い", "学説", "取扱い", "送り仮名"),
    ("行ないの善さ", "行い", "学説", "行ない", "送り仮名"),
    ("カントは義務を説いた", "イマヌエル・カント", "人物", "カント", "名前の一部"),
    ("理とは何か", "理（ロゴス）", "学説", "理", "括弧の前"),
    ("功利性の原理を説く", "功利性原理", "学説", "功利性の原理", "送り仮名"),
    ("功利性原理を説く", "功利性原理説", "学説", "功利性原理", "部分一致"),
])
def test_find_surface(page, label, kind, want, how):
    got = extraction.find_surface(label, kind, {1: page}, {"人物"})
    assert got == (want, how, 1)


def test_find_surface_none_when_too_little_overlap():
    assert extraction.find_surface("無我説", "学説", {1: "甲野の話"}, set()) is None
    assert extraction.find_surface("義務論的倫理", "学説", {1: "義務の話"}, set()) is None   # 2/3 に満たない


def test_label_fixed_to_page_text_and_recorded(monkeypatch):
    lectures = [lec(1, "前置き", "カントは義務を説いた")]
    concepts, _, info, fake = run(monkeypatch, lectures, [[c("kant", "イマヌエル・カント", "人物", 1)]])
    assert labels(concepts) == ["カント"]
    assert concepts["kant"]["sources"] == [{"lecture": 1, "page": 2}]   # 名前が書かれているページを出典にする
    assert info["label_fixes"] == [{"from": "イマヌエル・カント", "to": "カント", "how": "名前の一部",
                                    "lecture": 1, "page": 2, "merged_into": None}]
    assert fake.asks == []


def test_not_found_label_is_asked_once_per_lecture_and_verified(monkeypatch):
    lectures = [lec(1, "幸福の最大化を説く", "x", "徳の倫理")]
    concepts, _, info, fake = run(monkeypatch, lectures, [
        [c("kouri", "功利主義", "学説", 1)],
        [c("toku", "徳倫理学", "学説", 3), c("muga", "無我説", "学説", 3)],
    ], surfaces=[[
        {"index": 0, "surface": "幸福の最大化", "page": 1},
        {"index": 1, "surface": "徳の倫理", "page": 3},
        {"index": 2, "surface": "無我", "page": 3},          # 本文に無い → 確かめられない
    ]])
    assert len(fake.asks) == 1                                # 回ごとに 1 回だけ
    assert "[0] 功利主義" in fake.asks[0] and "[2] 無我説" in fake.asks[0]
    assert labels(concepts) == ["幸福の最大化", "徳の倫理"]
    assert info["surface_asked"] == 3 and info["surface_ask_resolved"] == 2
    assert [f["how"] for f in info["label_fixes"]] == ["聞き直し", "聞き直し"]
    # 見つからない概念は消さずに記録する
    assert [m["label"] for m in info["without_surface"]] == ["無我説"]
    assert info["without_surface"][0]["asked_surface"] == "無我"


def test_part_fix_colliding_with_known_concept_is_asked_not_merged(monkeypatch):
    lectures = [lec(1, "功利主義の話", "功利主義を続ける")]
    concepts, _, info, fake = run(monkeypatch, lectures, [
        [c("kouri", "功利主義", "学説", 1)],
        [c("kourisha", "功利主義者", "学説", 2)],         # 部分一致だと既出の「功利主義」と同じ表記になる
    ], pages_per_call=1, surfaces=[[{"index": 0, "surface": "", "page": 2}]])
    assert labels(concepts) == ["功利主義"]                    # 既出の概念に統合しない
    assert len(fake.asks) == 1
    assert [m["label"] for m in info["without_surface"]] == ["功利主義者"]   # 消さずに記録する


def test_known_concept_by_id_keeps_map_label(monkeypatch):
    lectures = [lec(1, "イマヌエル・カント"), lec(2, "カントの義務論")]
    concepts, _, info, _ = run(monkeypatch, lectures, [
        [c("kant", "イマヌエル・カント", "人物", 1)],
        [c("kant", "カント", "人物", 1)],                   # 既出の id で、このページの表記
    ])
    assert labels(concepts) == ["イマヌエル・カント"]
    assert concepts["kant"]["sources"] == [{"lecture": 1, "page": 1}, {"lecture": 2, "page": 1}]


def test_mention_without_name_does_not_drop_known_concept(monkeypatch):
    lectures = [lec(1, "乙説の話"), lec(2, "それについての続き")]
    concepts, _, info, fake = run(monkeypatch, lectures, [
        [c("otsu", "乙説", "学説", 1)],
        [c("otsu", "乙説", "学説", 1)],              # 第2回には名前が無い
    ])
    assert concepts["otsu"]["sources"] == [{"lecture": 1, "page": 1}]
    assert concepts["otsu"]["lectures"] == [1]
    assert info["mentions_without_name"] == 1 and fake.asks == []


def test_pages_out_of_chunk_are_ignored(monkeypatch):
    lectures = [lec(1, "甲野", "x", "甲野も")]
    concepts, _, _, _ = run(monkeypatch, lectures, [[c("kouno", "甲野", "人物", 1, 3)], []])
    assert concepts["kouno"]["sources"] == [{"lecture": 1, "page": 1}]


def test_zero_pages_per_call_means_one_call_per_lecture(monkeypatch):
    lectures = [lec(1, "甲野", "乙説", "丙説")]
    _, _, info, fake = run(monkeypatch, lectures, [[c("kouno", "甲野", "人物", 1)]], pages_per_call=0)
    assert info["calls"] == 1 and "p.1〜p.3" in fake.users[0]
