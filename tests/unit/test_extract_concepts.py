"""概念の抽出の部品（#101）: ページの分け方、本文の表記の探し方、括弧の直し。API は呼ばない。"""
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


@pytest.mark.parametrize("raw, want", [
    ("幸福」(エウダイモニア)", "幸福(エウダイモニア)"),
    ("「善意志", "善意志"),
    ("「善意志」", "「善意志」"),
    ("理(ロゴス", "理ロゴス"),
    ("『A』と「B」", "『A』と「B」"),
    ("(「A)」", "「A」"),
])
def test_drop_unpaired_brackets(raw, want):
    assert extraction.drop_unpaired_brackets(raw) == want


def test_fixed_surface_has_no_unpaired_bracket():
    # 括弧を無視して一致させると、本文の範囲の途中に閉じかぎ括弧が入る（#101 の取り込みで起きた形）
    got = extraction.find_surface("幸福（エウダイモニア）", "学説", {1: "「幸福」(エウダイモニア)とは"}, set())
    assert got == ("幸福(エウダイモニア)", "表記ゆれ", 1)
