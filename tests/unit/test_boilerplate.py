"""抽出に渡す本文から決まり文句とページ番号を除く（#119）。API は呼ばない。

数学の資料では、講師名が毎ページの見出しとつながって（'Application: OptimizationTomoo Matsumura0 / 12'）
抽出で講師が 54 本の関係の中心になった。確かめること:
- 半分以上のページに出る文字列を、行の区切りに頼らず見つけて除く。短い言い回しや日本語の本文は除かない
- 末尾のページ番号（「0 / 12」）を、隣のページと番号が続くときだけ除く。前の数字とつながっていても分ける
- 除くのは LLM に渡す本文（prompt_pages）だけ。pages.json・照合の本文（pages）はそのまま
- 何を除いたかを記録と graph.json の stats.boilerplate に残す
資料の本文は使わず、作った文で試す。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from quizmap import boilerplate, common, courses, extraction, vocab
from quizmap.common import Lecture, read_json, write_json

TOPICS = ["Derivatives and rates of change", "Optimization of a box volume", "Critical points and extrema",
          "Normal distribution and z scores", "Hypothesis testing basics", "Eigenvalues of a matrix",
          "Linear models for owls and rats", "Sampling distribution of the mean"]


WORDS = ("area slope limit tangent curve square corner volume sheet width height cost profit sample trial "
         "mean variance owl rat food growth matrix vector basis point line plane angle speed time rate "
         "graph value table data count model error test claim level").split()


def filler(seed: int, n: int = 40) -> str:
    """ページごとに違う、本文らしい語の並び（見出し・脚注が頭と末尾から離れるように長くする）。"""
    return " ".join(WORDS[(seed * 7 + k * 13 + k * k) % len(WORDS)] for k in range(n))


def math_lecture(number: int, n: int = 8) -> Lecture:
    """見出し＋講師名＋ページ番号が改行なしでつながったページ（1 ページ目は表紙で番号なし）。"""
    pages = [f"{TOPICS[number % len(TOPICS)]} The World of MathematicsTomoo Matsumura (Mathematics Major)"]
    for i in range(n - 1):
        body = f"Ex{i}: f(x) = {i}x + {number}. {filler(number * 10 + i)}"
        pages.append(f"{TOPICS[(number + i) % len(TOPICS)]}{body}Tomoo Matsumura{i} / {n - 1}")
    return Lecture(number=number, path=Path(f"{number}.pdf"), pages=pages)


ETHICS = ["倫理学とは何かを考える。カントは定言命法を説いたということである。",
          "功利主義はベンサムとミルによって唱えられたということである。",
          "徳倫理はアリストテレスに始まるということである。幸福とは何か。",
          "義務論と帰結主義の違いを考えるということである。",
          "ハイデガーは存在と時間を書いたということである。",
          "ヘーゲルの人倫は家族・市民社会・国家からなるということである。",
          "和辻哲郎は倫理を人間の学と呼んだということである。",
          "ロールズの正義論は公正としての正義を説くということである。"]


# ------------------------------------------------------------------ 決まり文句

def test_lecturer_name_glued_to_headings_is_found():
    lecs = [math_lecture(n) for n in range(1, 4)]
    found = boilerplate.find_repeated([p for l in lecs for p in l.pages])
    assert found == [{"text": "Tomoo Matsumura", "pages": 24}]


def test_short_phrases_and_japanese_body_are_kept():
    # 「ということである」は全ページに出るが 8 字で短い。本文は削らない
    lecs = [Lecture(1, Path("1.pdf"), ETHICS)]
    report = boilerplate.strip_boilerplate(lecs)
    assert report["repeated"] == [] and report["page_numbers"] == 0
    assert lecs[0].prompt_pages == ETHICS


def test_header_in_less_than_half_of_pages_is_kept():
    pages = [ETHICS[i] + ("授業の見出しの帯がここにある" if i < 3 else "") for i in range(8)]
    assert boilerplate.find_repeated(pages) == []


def test_few_pages_are_not_searched():
    pages = ["Same header line here. body %d" % i for i in range(boilerplate.MIN_PAGES - 1)]
    assert boilerplate.find_repeated(pages) == []


def test_numbers_only_are_not_boilerplate():
    pages = [f"text {i} 1234567890123" for i in range(10)]
    assert boilerplate.find_repeated(pages) == []


# ------------------------------------------------------------------ ページ番号

def test_page_numbers_glued_to_previous_digits_are_split():
    pages = ["Cover page", "Intro0 / 4", "y = x + 21 / 4", "Builds the same slide2 / 4", "Builds more2 / 4",
             "Last x_03 / 4", "4 / 4"]
    cuts = boilerplate.find_page_numbers(pages)
    assert [p[c:] if c is not None else None for p, c in zip(pages, cuts)] == \
        [None, "0 / 4", "1 / 4", "2 / 4", "2 / 4", "3 / 4", "4 / 4"]


def test_fractions_in_body_are_not_page_numbers():
    pages = ["half is 1/2", "a third is 1/3", "ratio 3/4 here", "nothing", "also 2/9 alone"]
    assert boilerplate.find_page_numbers(pages) == [None] * 5


def test_two_decks_in_one_lecture():
    pages = ["A0 / 3", "B1 / 3", "C2 / 3", "D3 / 3", "E0 / 2", "F1 / 2", "G2 / 2"]
    cuts = boilerplate.find_page_numbers(pages)
    assert all(c is not None for c in cuts)


# ------------------------------------------------------------------ 全体

def test_strip_boilerplate_changes_only_prompt_pages():
    lecs = [math_lecture(n) for n in range(1, 4)]
    before = [list(l.pages) for l in lecs]
    report = boilerplate.strip_boilerplate(lecs)
    assert [l.pages for l in lecs] == before
    assert report["page_numbers"] == 3 * 7 and report["pages"] == 24
    joined = " ".join(p for l in lecs for p in l.prompt_pages)
    assert "Matsumura" not in joined and " / 7" not in joined
    assert lecs[0].prompt_page(2).startswith(TOPICS[1])
    assert report["chars_removed"] > 0
    lines = boilerplate.report_lines(report)
    assert "Tomoo Matsumura" in lines[0] and "24 ページ" in lines[0] and any("ページ番号" in line for line in lines)


def test_prompt_page_falls_back_to_pages():
    lec = Lecture(1, Path("1.pdf"), ["a", "b"])
    assert lec.prompt_page(2) == "b"
    lec.prompt_pages = ["A", "B"]
    assert lec.prompt_page(2) == "B"
    lec.prompt_pages = ["A"]                         # 数が合わなければ使わない
    assert lec.prompt_page(2) == "b"


def test_prompts_use_prompt_pages():
    lec = Lecture(3, Path("3.pdf"), ["raw 1", "raw 2"], prompt_pages=["clean 1", "clean 2"])
    assert "clean 2" in common.lecture_as_prompt_text(lec) and "raw" not in common.lecture_as_prompt_text(lec)
    text = extraction._chunk_as_prompt_text(lec, [1, 2])
    assert "[第3回 p.2] clean 2" in text and "raw" not in text


# ------------------------------------------------------------------ 取り込みの流れ

@pytest.fixture
def ingest_fakes(monkeypatch, courses_dir):
    seen = {}

    def load_lecture_files(pairs):
        return [math_lecture(n) for _, n in sorted(pairs, key=lambda x: x[1])]

    def derive_vocab(client, model, lectures, course_hint, log=print):
        seen["vocab_prompt"] = common.lecture_as_prompt_text(lectures[0])
        return {"course": "x", "kinds": [], "relations": []}

    def run_extraction(client, model, vocab_, lectures, out_dir, log=print, step=lambda n: None, **kw):
        seen["chunk"] = extraction._chunk_as_prompt_text(lectures[0], [2])
        write_json(out_dir / "pages.json", [{"lecture": l.number, "page": i, "text": t}
                                            for l in lectures for i, t in enumerate(l.pages, 1)])
        graph = {"course": "x", "concepts": [], "relations": [], "stats": {"lectures": [1, 2]}}
        write_json(out_dir / "graph.json", graph)
        return graph

    monkeypatch.setenv("QUIZMAP_RAG", "off")
    monkeypatch.setattr(courses, "load_lecture_files", load_lecture_files)
    monkeypatch.setattr(courses, "make_client", lambda: object())
    monkeypatch.setattr(vocab, "derive_vocab", derive_vocab)
    monkeypatch.setattr(extraction, "run_extraction", run_extraction)
    return seen


def test_ingest_strips_for_prompts_and_records_it(ingest_fakes):
    d = courses.create_course("world of mathematics")
    info = courses.course_info(d)
    info["files"] = [{"name": f"{n}.pdf", "lecture": n, "use": True} for n in (1, 2)]
    courses.save_course_info(d, info)
    job = courses.Job(course_dir=str(d))
    courses._run(job, d, False, "fake-model")
    assert job.status == "done", job.error
    assert "Matsumura" not in ingest_fakes["vocab_prompt"] and "Matsumura" not in ingest_fakes["chunk"]
    assert any("Tomoo Matsumura" in line for line in job.log)
    assert any("ページ番号" in line for line in job.log)
    pages = read_json(d / "pages.json")
    assert sum("Tomoo Matsumura" in p["text"] for p in pages) == 16      # pages.json の本文はそのまま
    stats = read_json(d / "graph.json")["stats"]
    assert stats["lectures"] == [1, 2]
    assert stats["boilerplate"]["repeated"] == [{"text": "Tomoo Matsumura", "pages": 16}]
    assert stats["boilerplate"]["page_numbers"] == 14


def test_frequent_subject_word_in_body_is_kept():
    """統計の講義で 'distribution' が全ページの本文に何度も出ても、見出し・脚注ではないので除かない。"""
    pages = [f"Slide {i}: {filler(i, 20)} distribution {filler(i + 50, 15)} distribution {filler(i + 99, 20)}"
             for i in range(12)]
    assert boilerplate.find_repeated(pages) == []


def test_word_once_per_page_in_the_middle_is_kept():
    pages = [f"{filler(i, 25)} probability {filler(i + 40, 25)}" for i in range(12)]
    assert boilerplate.find_repeated(pages) == []


# ------------------------------------------------------------------ 決まり文句の残り（講師名の間に別の字が入ったもの）

def _with_name_remnants():
    lecs = [math_lecture(n) for n in range(1, 7)]           # 48 ページ。名前の残りは 3 ページ
    lecs[0].pages[3] += " impact on you Tomoo We Matsumura can begin"
    lecs[1].pages[2] += " file:///Users/tomoomatsumura/Downloads/board.png"
    lecs[2].pages[4] += " 1 −oo Matsumura 2 4"
    return lecs


def test_name_remnants_are_removed_from_prompts():
    lecs = _with_name_remnants()
    report = boilerplate.strip_boilerplate(lecs)
    assert [w["text"] for w in report["words"]] == ["Tomoo", "Matsumura", "TomooMatsumura"]
    joined = " ".join(p for l in lecs for p in l.prompt_pages)
    assert not re.search(r"(?i)tomoo|matsumura", joined)
    assert "impact on you We can begin" in joined and "file:///Users/ /Downloads/board.png" in joined
    assert any("ほとんど出ない語" in line and "Matsumura" in line for line in boilerplate.report_lines(report))


def test_ordinary_header_words_are_kept_in_body():
    """見出しの帯 'NATURAL LANGUAGE PROCESSING · ICU' を除いても、本文の表の見出し 'NATURAL LANGUAGE' や
    'Natural language' は残す（小文字の natural・language がふつうの語として出るため）。"""
    pages = [f"{filler(i)} NATURAL LANGUAGE PROCESSING · ICU" for i in range(10)]
    pages[3] = "Natural language (vs. artificial language) NATURAL LANGUAGEARTIFICIAL LANGUAGE " + pages[3]
    pages[5] = "language processing pipelines " + pages[5]
    lecs = [Lecture(1, Path("1.pdf"), pages)]
    report = boilerplate.strip_boilerplate(lecs)
    assert report["repeated"] == [{"text": "NATURAL LANGUAGE PROCESSING · ICU", "pages": 10}]
    assert report["words"] == []
    assert lecs[0].prompt_pages[3].startswith("Natural language (vs. artificial language) NATURAL LANGUAGEARTIFICIAL")


def test_word_often_outside_the_boilerplate_is_kept():
    lecs = _with_name_remnants()
    for i in range(1, 7):                                  # 名前の外にも何度も出る語は、決まり文句の残りとはみなさない
        lecs[0].pages[i] += " Matsumura theorem"
    report = boilerplate.strip_boilerplate(lecs)
    assert "Matsumura" not in [w["text"] for w in report["words"]]
    assert "Matsumura theorem" in " ".join(lecs[0].prompt_pages)
