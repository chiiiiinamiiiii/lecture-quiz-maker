"""quizmap.extraction.extract_graph（#107: 概念と関係をかたまりごとに 1 回で取り、かたまりを並列に呼ぶ）。

LLM は extraction.call_json を差し替える。API は呼ばない。
"""
from __future__ import annotations

import re
import threading
import time
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
        {"name": "文献", "definition": "本", "examples": ["本"], "cited_work": True},
    ],
    "relations": [
        {"label": "提唱", "definition": "人物が学説を唱えた", "example": "A → B",
         "from_kinds": ["人物"], "to_kinds": ["学説"]},
        {"label": OTHER_LABEL, "definition": "", "example": ""},
    ],
}


def lec(number, *pages):
    return Lecture(number=number, path=Path(f"第{number}回.pdf"), pages=list(pages))


def c(cid, label, kind, *pages):
    return {"id": cid, "label": label, "kind": kind, "definition": "説明", "pages": list(pages)}


def rel(f, t, page, evidence, label="提唱"):
    return {"from": f, "to": t, "label": label, "page": page, "evidence": evidence}


class FakeLLM:
    """かたまり（回, 最初のページ, 最後のページ）ごとに answers を返す。聞き直しは surfaces[回] を返す。"""

    def __init__(self, answers, surfaces=None, delay=0.0):
        self.answers, self.surfaces, self.delay = answers, surfaces or {}, delay
        self.chunks, self.asks = [], []
        self.lock = threading.Lock()
        self.active = self.max_active = 0

    def __call__(self, client, *, model, system, user, schema, **kw):
        lecture = int(re.search(r"第(\d+)回", user).group(1))
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(self.delay)
        with self.lock:
            self.active -= 1
            if schema is extraction.SURFACE_SCHEMA:
                self.asks.append(user)
                return {"answers": self.surfaces[lecture]}
            m = re.search(r"p\.(\d+)〜p\.(\d+)", user)
            key = (lecture, int(m.group(1)), int(m.group(2)))
            self.chunks.append(key)
        concepts, relations = self.answers.get(key, ([], []))
        return {"concepts": concepts, "relations": relations}


def run(monkeypatch, lectures, answers, size=2, overlap=0, surfaces=None, workers=4, delay=0.0, vocab=VOCAB):
    fake = FakeLLM(answers, surfaces, delay)
    monkeypatch.setattr(extraction, "call_json", fake)
    logs = []
    concepts, relations, unmatched, info, dropped, adjusted = extraction.extract_graph(
        None, "m", "テスト", vocab, lectures, log=logs.append, pages_per_call=size, overlap=overlap, workers=workers)
    return concepts, relations, info, dropped, fake, logs


def labels(concepts):
    return sorted(c["label"] for c in concepts.values())


@pytest.mark.parametrize("n, size, overlap, want", [
    (8, 4, 1, [(1, 4), (4, 8)]),
    (9, 4, 1, [(1, 3), (3, 6), (6, 9)]),
    (9, 4, 0, [(1, 3), (4, 6), (7, 9)]),
    (3, 4, 1, [(1, 3)]),
    (6, 0, 1, [(1, 6)]),
])
def test_extraction_chunks(n, size, overlap, want):
    assert extraction.extraction_chunks(n, size, overlap) == want


def test_merge_key_ignores_nfkc_spaces_and_middle_dots():
    assert extraction.merge_key("イマヌエル・カント") == extraction.merge_key("イマヌエル カント") \
        == extraction.merge_key("イマヌエル＝カント")
    assert extraction.merge_key("ＡＢＣ 理論") == extraction.merge_key("ABC理論")
    assert extraction.merge_key("理(ロゴス)") != extraction.merge_key("理")


def test_one_call_per_chunk_and_chunks_run_in_parallel(monkeypatch):
    lectures = [lec(1, "a", "b", "c", "d"), lec(2, "e", "f")]
    _, _, info, _, fake, logs = run(monkeypatch, lectures, {}, size=2, delay=0.05)
    assert sorted(fake.chunks) == [(1, 1, 2), (1, 3, 4), (2, 1, 2)]
    assert info["chunks"] == 3 and info["calls"] == 3
    assert fake.max_active >= 2                                   # 順番待ちをしない
    assert sum(l.startswith("概念 第") for l in logs) == 2         # 進み具合の行は回ごとに 1 行
    assert sum(l.startswith("関係 第") for l in logs) == 2


def test_merge_by_page_surface_and_ids_in_chunk_order(monkeypatch):
    lectures = [lec(1, "イマヌエル・カントの話", "乙説"), lec(2, "イマヌエル カントふたたび")]
    answers = {
        (1, 1, 1): ([c("kant", "イマヌエル・カント", "人物", 1)], []),
        (1, 2, 2): ([c("otsu", "乙説", "学説", 2)], []),
        (2, 1, 1): ([c("i_kant", "イマヌエル カント", "人物", 1)], []),
    }
    concepts, _, info, _, _, _ = run(monkeypatch, lectures, answers, size=1)
    assert set(concepts) == {"kant", "otsu"}                       # id は最初に出たかたまりの案
    assert concepts["kant"]["label"] == "イマヌエル・カント"
    assert concepts["kant"]["sources"] == [{"lecture": 1, "page": 1}, {"lecture": 2, "page": 1}]
    assert concepts["kant"]["lectures"] == [1, 2]
    assert info["merged_mentions"] == 1 and info["merged_variants"] == 1
    assert info["new_by_lecture"] == {1: 2, 2: 0}


def test_id_collision_between_different_labels(monkeypatch):
    lectures = [lec(1, "義務の話", "義務論の話")]
    answers = {(1, 1, 1): ([c("gimu", "義務", "学説", 1)], []),
               (1, 2, 2): ([c("gimu", "義務論", "学説", 2)], [])}
    concepts, _, _, _, _, _ = run(monkeypatch, lectures, answers, size=1)
    assert {cid: x["label"] for cid, x in concepts.items()} == {"gimu": "義務", "gimu_": "義務論"}


def test_relation_ends_by_label_are_mapped_to_merged_ids(monkeypatch):
    lectures = [lec(1, "甲野は乙説を唱えた")]
    answers = {(1, 1, 1): ([c("kouno", "甲野", "人物", 1), c("otsu", "乙説", "学説", 1)],
                           [rel("甲野", "乙説", 1, "甲野は乙説を唱えた")])}
    concepts, relations, _, _, _, _ = run(monkeypatch, lectures, answers)
    assert [(r["from"], r["to"], r["label"], r["source"]) for r in relations] == \
        [("kouno", "otsu", "提唱", {"lecture": 1, "page": 1})]


def test_relation_end_fixed_to_page_text_still_resolves(monkeypatch):
    lectures = [lec(1, "カントは義務論を唱えた")]
    answers = {(1, 1, 1): ([c("kant", "イマヌエル・カント", "人物", 1), c("gimuron", "義務論", "学説", 1)],
                           [rel("イマヌエル・カント", "義務論", 1, "カントは義務論を唱えた")])}
    concepts, relations, info, _, _, _ = run(monkeypatch, lectures, answers)
    assert concepts["kant"]["label"] == "カント"
    assert [(r["from"], r["to"]) for r in relations] == [("kant", "gimuron")]
    assert info["label_fix_by_how"] == {"名前の一部": 1}


def test_relation_end_named_in_chunk_but_listed_elsewhere(monkeypatch):
    # 甲野 は p.1 のかたまりでだけ挙げられたが、p.2 の本文にも名前がある（#106 の工夫）
    lectures = [lec(1, "甲野の紹介", "甲野は丙説を唱えた")]
    answers = {(1, 1, 1): ([c("kouno", "甲野", "人物", 1)], []),
               (1, 2, 2): ([c("hei", "丙説", "学説", 2)], [rel("甲野", "丙説", 2, "甲野は丙説を唱えた")])}
    _, relations, _, _, _, _ = run(monkeypatch, lectures, answers, size=1)
    assert [(r["from"], r["to"]) for r in relations] == [("kouno", "hei")]


def test_relation_with_unknown_end_or_page_outside_chunk_is_dropped(monkeypatch):
    lectures = [lec(1, "甲野は乙説を唱えた", "甲野と乙説")]
    answers = {(1, 1, 1): ([c("kouno", "甲野", "人物", 1), c("otsu", "乙説", "学説", 1)],
                           [rel("甲野", "無我説", 1, "甲野は乙説を唱えた"), rel("甲野", "乙説", 2, "甲野と乙説")])}
    _, relations, _, dropped, _, _ = run(monkeypatch, lectures, answers, size=1)
    assert relations == []
    assert dropped["存在しない概念"] == 1 and dropped["ページ番号が範囲外"] == 1


def test_overlap_page_duplicates_merge(monkeypatch):
    lectures = [lec(1, "x", "甲野は乙説を唱えた", "z")]
    both = ([c("kouno", "甲野", "人物", 2), c("otsu", "乙説", "学説", 2)], [rel("甲野", "乙説", 2, "甲野は乙説を唱えた")])
    concepts, relations, info, dropped, fake, _ = run(monkeypatch, lectures, {(1, 1, 2): both, (1, 2, 3): both},
                                                     size=2, overlap=1)
    assert sorted(fake.chunks) == [(1, 1, 2), (1, 2, 3)]
    assert len(concepts) == 2 and info["merged_mentions"] == 2
    assert len(relations) == 1 and dropped["重複"] == 1


def test_not_found_label_is_asked_once_per_lecture(monkeypatch):
    lectures = [lec(1, "幸福の最大化を説く", "徳の倫理"), lec(2, "甲野")]
    answers = {(1, 1, 1): ([c("kouri", "功利主義", "学説", 1)], []),
               (1, 2, 2): ([c("toku", "徳倫理学", "学説", 2), c("muga", "無我説", "学説", 2)], []),
               (2, 1, 1): ([c("kouno", "甲野", "人物", 1)], [])}
    surfaces = {1: [{"index": 0, "surface": "幸福の最大化", "page": 1},
                    {"index": 1, "surface": "徳の倫理", "page": 2},
                    {"index": 2, "surface": "無我", "page": 2}]}
    concepts, _, info, _, fake, _ = run(monkeypatch, lectures, answers, size=1, surfaces=surfaces)
    assert len(fake.asks) == 1                                    # 聞き直しが要る回だけ、1 回ずつ
    assert labels(concepts) == ["幸福の最大化", "徳の倫理", "甲野"]
    assert info["surface_asked"] == 3 and info["surface_ask_resolved"] == 2
    assert info["calls"] == 3 + 1
    assert [m["label"] for m in info["without_surface"]] == ["無我説"]   # 消さずに記録する


def test_part_fix_colliding_with_other_concept_is_asked(monkeypatch):
    lectures = [lec(1, "功利主義の話", "功利主義を続ける")]
    answers = {(1, 1, 1): ([c("kouri", "功利主義", "学説", 1)], []),
               (1, 2, 2): ([c("kourisha", "功利主義者", "学説", 2)], [])}
    concepts, _, info, _, fake, _ = run(monkeypatch, lectures, answers, size=1,
                                         surfaces={1: [{"index": 0, "surface": "", "page": 2}]})
    assert labels(concepts) == ["功利主義"]
    assert len(fake.asks) == 1
    assert [m["label"] for m in info["without_surface"]] == ["功利主義者"]


def test_run_extraction_drops_relations_to_cited_works(monkeypatch, tmp_path):
    lectures = [lec(1, "甲野は『乙』を書き、丙説を唱えた")]
    answers = {(1, 1, 1): ([c("kouno", "甲野", "人物", 1), c("otsu", "『乙』", "文献", 1), c("hei", "丙説", "学説", 1)],
                           [rel("甲野", "『乙』", 1, "甲野は『乙』を書き", label=OTHER_LABEL),
                            rel("甲野", "丙説", 1, "丙説を唱えた")])}
    monkeypatch.setattr(extraction, "call_json", FakeLLM(answers))
    g = extraction.run_extraction(None, "m", VOCAB, lectures, tmp_path, slides=False, log=lambda m: None)
    assert sorted(c["id"] for c in g["concepts"]) == ["hei", "kouno"]
    assert [(r["from"], r["to"]) for r in g["relations"]] == [("kouno", "hei")]
    s = g["stats"]
    assert s["dropped"]["端が出典の文献"] == 1
    assert s["extract_chunks"] == 1 and s["extract_calls"] == 1
    assert s["isolated_concepts"] == 0 and s["removed_cited_work_concepts"] == 1
    assert g["concepts_without_surface"] == []
