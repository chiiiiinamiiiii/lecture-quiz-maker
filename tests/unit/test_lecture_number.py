"""quizmap.common.lecture_number_of: ファイル名から講義回を読む（§9-3）。"""
from pathlib import Path

import pytest

from quizmap.common import lecture_number_of


@pytest.mark.parametrize("name, expected", [
    ("第10回 倫理学の世界.pdf", 10),
    ("第１回.pdf", 1),                         # 全角数字（NFKC で読む）
    ("【配布用】倫理学の世界 第2回.pdf", 2),
    ("week3.pdf", 3),
    ("Lecture 03.pdf", 3),
    ("lec_12.pdf", 12),
    ("05_intro.pdf", 5),
    ("2026A GEH032 第10回.pdf", 10),            # 年度があっても「第N回」を優先する
])
def test_reads_lecture_number(name, expected):
    assert lecture_number_of(Path(name)) == expected


@pytest.mark.parametrize("name", [
    "エンデ『モモ』第６章.pdf",                 # 読書課題。「章」は講義回ではない
    "2026A GEH032 資料.pdf",                    # 年度の数字だけ
    "2026.pdf",
    "syllabus.pdf",
])
def test_does_not_read(name):
    assert lecture_number_of(Path(name)) is None
