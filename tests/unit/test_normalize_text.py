"""§9-2 の改行の除き方（Refs #119）。日本語に合わせて改行を詰めていたので、英語の資料で語がつながった
（significancelevel、beendangered など 130 種類）。確かめること:
- ラテン文字・数字どうしの改行は空白にする。行末のハイフンで分けた語は詰める
- 日本語の本文は今までどおり詰める。日本語のページでは、数字どうし・1 字の箇条書きの記号は詰め、
  2 字以上の英語・ドイツ語などの語どうしだけ空白にする
- 改行の前後の空白は今までどおり 1 つにまとめる
"""
from __future__ import annotations

import re
import unicodedata

import pytest

from quizmap import common
from quizmap.common import normalize_text


def old_normalize(text: str) -> str:
    """#119 より前の normalize_text（改行を詰めるだけ）。"""
    text = unicodedata.normalize("NFKC", text).translate(common.RADICAL_SUPPLEMENT_MAP)
    text = text.replace("\r", "").replace("\n", "")
    return re.sub(r"[ \t　]+", " ", text).strip()


@pytest.mark.parametrize("raw,expected", [
    ("at the significance\nlevel of 0.05", "at the significance level of 0.05"),
    ("species have been\nendangered", "species have been endangered"),
    ("an exam-\nple of a func-\n tion", "an example of a function"),
    ("Smith-\nJones test", "Smith-Jones test"),
    ("rate,\nand slope.\nNext", "rate, and slope. Next"),
    ("x = 2\ny = 3", "x = 2 y = 3"),
    ("Chapter 4\n12", "Chapter 4 12"),
    ("café\nnoir", "café noir"),
    ("line one\r\nline two", "line one line two"),
    ("trailing  \n  spaces", "trailing spaces"),
    ("a\n\n\nb", "a b"),
])
def test_latin_line_breaks_become_spaces(raw, expected):
    assert normalize_text(raw) == expected


@pytest.mark.parametrize("raw", [
    "カントは定言\n命法を説いた。\n功利主義は\nベンサムに始まる。",
    "倫理学\n(Kant)\nの講義",
    "第3回\n2026/9/14\nu 箇条書きの記号",
    "幸福とは何か\n4\n2\n次のページ",
    "問題u\nu 次の項目u\n18世紀",
    "ＡＢＣ\n全角の英字のあとに日本語",
])
def test_japanese_text_unchanged(raw):
    assert normalize_text(raw) == old_normalize(raw)


def test_latin_words_inside_japanese_page_get_a_space():
    raw = "ケアの倫理（Carol\nGilligan）は、完全義務（perfect\nduties）と区別される。"
    assert normalize_text(raw) == "ケアの倫理(Carol Gilligan)は、完全義務(perfect duties)と区別される。"


def test_japanese_page_keeps_digits_joined_but_english_page_does_not():
    ja = "これは日本語のページで、番号が続く。\n1\n2"
    en = "This is an English page with numbers.\n1\n2"
    assert normalize_text(ja).endswith("続く。12")
    assert normalize_text(en).endswith("numbers. 1 2")


def test_clause_end_then_latin_on_japanese_page_is_joined():
    raw = "日本語の本文がここに長く続いている(注)\nu 次の項目"
    assert normalize_text(raw) == old_normalize(raw)


def test_spaces_next_to_line_break_are_kept_as_one():
    # 化けた文字の間の空白を消さない（今までどおり 1 つにまとめる）
    raw = "製 \n ゙ 心"
    assert normalize_text(raw) == old_normalize(raw)


def test_no_newline_is_unchanged():
    for raw in ["倫理学の世界", "A plain sentence.", "  spaced   out  ", ""]:
        assert normalize_text(raw) == old_normalize(raw)
