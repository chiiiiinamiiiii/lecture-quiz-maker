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
        None, "m", "テスト", vocab, lectures, log=logs.append, pages_per_call=size, overlap=overlap, workers=workers,
        chunk_chars=0)                                          # 既存のテストはページ数で分ける
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
    # #114: 末尾の括弧書き（訳語・原語）とかぎ括弧の有無は無視する。括弧の中の語と『』は別のまま
    assert extraction.merge_key("理(ロゴス)") == extraction.merge_key("理") == extraction.merge_key("理（ロゴス）")
    assert extraction.merge_key("自律(autonomy)") == extraction.merge_key("自律")
    assert extraction.merge_key("「円環的な時間」") == extraction.merge_key("円環的な時間")
    assert extraction.merge_key("理(ロゴス)") != extraction.merge_key("ロゴス")
    assert extraction.merge_key("理(ロゴス)を伴う活動") != extraction.merge_key("理を伴う活動")
    assert extraction.merge_key("『国家』") != extraction.merge_key("国家")


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


# ---------------------------------------------------------------- #114

def test_variants_with_gloss_or_quotes_merge_and_show_most_listed_label(monkeypatch):
    """「幸福(エウダイモニアー)」と「幸福」、「「円環的な時間」」と「円環的な時間」を 1 つにまとめる（#114）。"""
    lectures = [lec(1, "甲野は幸福(エウダイモニアー)を唱えた。「円環的な時間」", "幸福とは何か。円環的な時間。",
                    "幸福について。")]
    answers = {
        (1, 1, 1): ([c("kouno", "甲野", "人物", 1), c("eudaimonia", "幸福(エウダイモニアー)", "学説", 1),
                     c("enkan", "「円環的な時間」", "学説", 1)],
                    [rel("甲野", "幸福(エウダイモニアー)", 1, "甲野は幸福(エウダイモニアー)を唱えた")]),
        (1, 2, 2): ([c("koufuku", "幸福", "学説", 2), c("enkan2", "円環的な時間", "学説", 2)], []),
        (1, 3, 3): ([c("koufuku", "幸福", "学説", 3)], []),
    }
    concepts, relations, info, dropped, _, _ = run(monkeypatch, lectures, answers, size=1)
    assert labels(concepts) == ["円環的な時間", "幸福", "甲野"]       # 多く挙がった表記、同じなら短いほう
    happy = next(c for c in concepts.values() if c["label"] == "幸福")
    assert happy["id"] == "eudaimonia"                                  # id は最初に出た挙げ方から
    assert [s["page"] for s in happy["sources"]] == [1, 2, 3]
    assert relations == [{"from": "kouno", "to": "eudaimonia", "label": "提唱", "source": {"lecture": 1, "page": 1}}]
    assert info["merged_variants"] == 2
    assert sorted(map(sorted, info["merged_variant_examples"])) == [["「円環的な時間」", "円環的な時間"],
                                                                   ["幸福", "幸福(エウダイモニアー)"]]


def test_relation_end_resolves_by_any_merged_surface(monkeypatch):
    """ほかのかたまりで挙がった概念は、地図に出す表記でなくても、まとめた表記のどれかが本文にあればつなぐ。"""
    lectures = [lec(1, "円環的な時間。円環的な時間。", "甲野は「円環的な時間」を唱えた。")]
    answers = {
        (1, 1, 1): ([c("enkan", "円環的な時間", "学説", 1)], []),
        (1, 2, 2): ([c("kouno", "甲野", "人物", 2)], [rel("甲野", "「円環的な時間」", 2, "甲野は「円環的な時間」を唱えた")]),
    }
    concepts, relations, *_ = run(monkeypatch, lectures, answers, size=1)
    assert [(r["from"], r["to"]) for r in relations] == [("kouno", "enkan")]


@pytest.mark.parametrize("page, evidence, want", [
    ("甲野は乙説を唱えた", "甲野は乙説を唱えた", "exact"),
    ("u 甲野(こうの)は「乙説(otsu)」を唱えた", "甲野は乙説を唱えた", "loose"),   # 箇条書きの記号・括弧書き・かぎ括弧
    ("2026/9/7 甲野は\n乙説を唱えた", "2026/9/7甲野は乙説を唱えた", "exact"),
    ("2026/9/7 見出し u 甲野は乙説を唱えた", "見出し甲野は乙説を唱えた", "loose"),  # 日付と記号を飛ばす
    ("甲野は乙説を唱えた。丙は丁説を退けた", "甲野は丁説を退けた", None),           # 離れた箇所をつながない
    ("甲野は乙説を唱えた", "甲野は乙説を強く唱えた", None),                         # 文字を足さない
    ("義務の分類u格率を普遍化する", "義務の分類格率を普遍化する", "loose"),       # 全角文字にはさまれた u
    ("Kant uses autonomy", "Kantusesautonomy", "exact"),
    ("you u know", "youknow", None),
    ("P(x, y) = R(x, y) − C(x, y)", "P(x, y) = R(x, y) - C(x, y)", "loose"),   # マイナスとハイフン（#119）
    ("ages 20–30 years", "ages 20-30 years", "loose"),                      # en ダッシュ
    ("Euler’s number e", "Eulers number e", "loose"),                       # アポストロフィ
    ("Student’s t-distribution", "Student's t-distribution", "loose"),
    ("R(x, y) − C(x, y)", "R(x, y) + C(x, y)", None),                       # 別の記号は通さない
])
def test_evidence_in(page, evidence, want):
    assert extraction.evidence_in(evidence, page) == want


def test_loose_evidence_keeps_relation_and_is_counted(monkeypatch):
    lectures = [lec(1, "u 甲野(こうの)は「乙説」を唱えた")]
    answers = {(1, 1, 1): ([c("kouno", "甲野", "人物", 1), c("otsu", "乙説", "学説", 1)],
                           [rel("甲野", "乙説", 1, "甲野は乙説を唱えた")])}
    concepts, relations, info, dropped, _, _ = run(monkeypatch, lectures, answers, size=1)
    assert [(r["from"], r["to"]) for r in relations] == [("kouno", "otsu")]
    assert info["loose_evidence"] == 1 and "evidence が本文に無い" not in dropped


def test_dropped_relations_are_recorded_without_evidence(monkeypatch):
    lectures = [lec(1, "甲野は乙説を唱えた")]
    answers = {(1, 1, 1): ([c("kouno", "甲野", "人物", 1), c("otsu", "乙説", "学説", 1)],
                           [rel("甲野", "乙説", 1, "本文に無い文"), rel("甲野", "無い説", 1, "甲野は乙説を唱えた")])}
    _, relations, info, dropped, _, _ = run(monkeypatch, lectures, answers, size=1)
    assert relations == []
    assert info["dropped_relations"] == [
        {"reason": "evidence が本文に無い", "from": "甲野", "to": "乙説", "label": "提唱", "lecture": 1, "page": 1},
        {"reason": "存在しない概念", "from": "甲野", "to": "無い説", "label": "提唱", "lecture": 1, "page": 1},
    ]
    assert dropped == {"evidence が本文に無い": 1, "存在しない概念": 1}


def test_prompt_asks_to_relate_listed_concepts_and_copy_evidence_verbatim(monkeypatch):
    _, _, _, _, fake, _ = run(monkeypatch, [lec(1, "甲野")], {}, size=1)
    user = extraction.EXTRACT_USER
    assert "どの関係にも入っていない概念を見直し" in user
    assert "項目のそれぞれを、まとめる側の概念と結ぶ" in user
    assert "括弧書き（原語・訳語・読み）、かぎ括弧、記号も省かない" in user
    assert "離れた箇所をつながない" in user


# ---------------------------------------------------------------- 直した表記が別の概念と重なる（#101）

def _toku_case(p3: str):
    lectures = [lec(1, "徳とは何か。", "アレテー(徳)の倫理", p3)]
    answers = {(1, 1, 1): ([c("toku", "徳", "学説", 1)], []),
               (1, 2, 2): ([c("toku_rinri", "徳倫理学", "学説", 2)], []),
               (1, 3, 3): ([], [])}
    surfaces = {1: [{"index": 0, "surface": "徳", "page": 2}]}    # 聞き直しが、短い別の概念の表記を返す
    return lectures, answers, surfaces


def test_shortened_fix_into_other_concept_moves_to_page_with_original_name(monkeypatch):
    """「徳倫理学」の聞き直しが「徳」を返しても「徳」にまとめず、同じ回の元の名前のあるページに付け替える。"""
    lectures, answers, surfaces = _toku_case("徳倫理学の復権")
    concepts, _, info, _, _, logs = run(monkeypatch, lectures, answers, size=1, surfaces=surfaces)
    assert labels(concepts) == ["徳", "徳倫理学"]
    assert concepts["toku_rinri"]["sources"] == [{"lecture": 1, "page": 3}]
    assert concepts["toku"]["sources"] == [{"lecture": 1, "page": 1}]    # 「徳」に p.2 の出典を足さない
    assert info["surface_collisions_relocated"] == 1 and info["surface_collisions_without"] == 0
    assert any(f["how"] == "同じ回の別のページ" and f["to"] == "徳倫理学" for f in info["label_fixes"])
    assert info["without_surface"] == []


def test_shortened_fix_into_other_concept_without_original_name_is_recorded(monkeypatch):
    lectures, answers, surfaces = _toku_case("ほかの話")
    concepts, _, info, _, _, _ = run(monkeypatch, lectures, answers, size=1, surfaces=surfaces)
    assert labels(concepts) == ["徳"]
    assert concepts["toku"]["sources"] == [{"lecture": 1, "page": 1}]   # 「徳」に出典を足さない
    assert info["surface_collisions_without"] == 1
    [w] = info["without_surface"]
    assert w["label"] == "徳倫理学" and w["collided_with"] == "徳"


def test_shortened_fix_without_other_concept_is_kept(monkeypatch):
    """短くなっても、同じ表記の別の概念が無ければ、これまでどおりその表記にする。"""
    lectures = [lec(1, "ほか", "アレテー(徳)の倫理")]
    answers = {(1, 1, 1): ([], []), (1, 2, 2): ([c("toku_rinri", "徳倫理学", "学説", 2)], [])}
    concepts, _, info, _, _, _ = run(monkeypatch, lectures, answers, size=1,
                                     surfaces={1: [{"index": 0, "surface": "徳", "page": 2}]})
    assert labels(concepts) == ["徳"]
    assert info["surface_collisions_relocated"] == info["surface_collisions_without"] == 0


# ---------------------------------------------------------------- 文字数でかたまりを作る（#107）

@pytest.mark.parametrize("sizes, chars, max_pages, want", [
    ([100] * 10, 300, 8, [(1, 3), (4, 6), (7, 10)]),           # 最後の 1 ページ（100 < 150）は前に合わせる
    ([100] * 9, 300, 8, [(1, 3), (4, 6), (7, 9)]),
    ([10] * 20, 1000, 8, [(1, 8), (9, 16), (17, 20)]),          # 文字が少なければページ数の上限で切る
    ([500, 10, 10, 500, 10], 400, 8, [(1, 1), (2, 5)]),         # 1 ページで目安を超えるページは単独。小さい最後は前へ
    ([100, 100, 100, 20], 300, 3, [(1, 3), (4, 4)]),            # 合わせると上限を超えるなら合わせない
    ([50], 300, 8, [(1, 1)]),
    ([], 300, 8, []),
])
def test_char_chunks(sizes, chars, max_pages, want):
    texts = ["あ" * n for n in sizes]
    assert extraction.char_chunks(texts, chars, max_pages) == want


def test_char_chunks_ignore_spaces_and_overlap_is_kept():
    texts = ["あ " * 100, "い\n" * 100, "う" * 100, "え" * 100]
    assert extraction.char_chunks(texts, 200, 8) == [(1, 2), (3, 4)]
    assert extraction.extraction_chunks(4, 4, 1, page_texts=texts, chars=200) == [(1, 2), (2, 4)]
    assert extraction.extraction_chunks(4, 2, 0, page_texts=texts, chars=0) == [(1, 2), (3, 4)]   # 0 ならページ数


def test_extract_graph_chunks_by_chars(monkeypatch):
    lectures = [lec(1, "甲" * 50, "乙" * 50, "丙" * 50, "丁" * 50, "戊" * 50)]
    fake = FakeLLM({})
    monkeypatch.setattr(extraction, "call_json", fake)
    *_, info, _, _ = extraction.extract_graph(None, "m", "テスト", VOCAB, lectures, log=lambda s: None,
                                              overlap=1, chunk_chars=100, max_pages=8)
    assert sorted(fake.chunks) == [(1, 1, 2), (1, 2, 4), (1, 4, 5)]
    assert info["chunks"] == 3 and info["chunk_chars"] == 100 and info["max_pages"] == 8


# ---------------------------------------------------------------- 数学の資料（#119）

def test_merge_key_ignores_latin_case_but_not_for_short_symbols():
    assert extraction.merge_key("Derivative") == extraction.merge_key("derivative")
    assert extraction.merge_key("Critical Points") == extraction.merge_key("Critical points")
    assert extraction.merge_key("CLT") == extraction.merge_key("clt")
    assert extraction.merge_key("P") != extraction.merge_key("p")           # 3 字未満（記号）は区別する
    assert extraction.merge_key("H0") != extraction.merge_key("h0")


def test_case_variants_merge_with_most_listed_label_and_kind(monkeypatch):
    lectures = [lec(1, "The Normal Distribution.", "normal distribution here", "normal distribution again")]
    answers = {
        (1, 1, 1): ([c("nd", "Normal Distribution", OTHER_LABEL, 1)], []),
        (1, 2, 2): ([c("nd2", "normal distribution", "学説", 2)], []),
        (1, 3, 3): ([c("nd3", "normal distribution", "学説", 3)], []),
    }
    concepts, _, info, _, _, _ = run(monkeypatch, lectures, answers, size=1)
    assert labels(concepts) == ["normal distribution"]
    (nd,) = concepts.values()
    assert nd["id"] == "nd" and nd["kind"] == "学説"                       # 種類も多く挙がったほう
    assert [s["page"] for s in nd["sources"]] == [1, 2, 3]


def test_majority_kind_prefers_vocab_kind_over_other_and_first_on_tie():
    assert extraction._majority_kind([OTHER_LABEL, OTHER_LABEL, "学説"]) == "学説"
    assert extraction._majority_kind(["人物", "学説"]) == "人物"
    assert extraction._majority_kind(["学説", "人物", "人物"]) == "人物"


@pytest.mark.parametrize("evidence", ["", "   ", None])
def test_relation_with_empty_evidence_is_dropped(monkeypatch, evidence):
    lectures = [lec(1, "甲野は乙説を唱えた。甲野")]
    answers = {(1, 1, 1): ([c("kouno", "甲野", "人物", 1), c("otsu", "乙説", "学説", 1)],
                           [rel("甲野", "乙説", 1, evidence)])}
    _, relations, info, dropped, _, _ = run(monkeypatch, lectures, answers, size=1)
    assert relations == []
    assert dropped["evidence が空"] == 1
    assert info["dropped_relations"][0]["reason"] == "evidence が空"


def test_prompt_excludes_formulas_variables_and_software():
    user = extraction.EXTRACT_USER
    assert "数式・等式そのもの、変数や記号の名前、数値の例" in user
    assert "ソフト・ウェブツール・計算サイトの名前は" in user
    assert "数式や記号が表す概念の名前" in user


def test_formula_examples_are_left_out_of_the_prompt(monkeypatch):
    vocab = {**VOCAB, "kinds": VOCAB["kinds"] + [
        {"name": "モデル", "definition": "数理モデル", "examples": ["y = mx + b", "線形モデル", "f(x) → g(x)"]},
        {"name": "式", "definition": "式だけ", "examples": ["a = b"]}]}
    fake = FakeLLM({})
    monkeypatch.setattr(extraction, "call_json", fake)
    seen = []
    orig = fake.__call__

    def spy(client, **kw):
        seen.append(kw["user"])
        return orig(client, **kw)
    monkeypatch.setattr(extraction, "call_json", spy)
    extraction.extract_graph(None, "m", "テスト", vocab, [lec(1, "甲野")], log=lambda *_: None,
                             pages_per_call=1, overlap=0, chunk_chars=0)
    assert "- モデル: 数理モデル（例: 線形モデル）" in seen[0]
    assert "- 式: 式だけ\n" in seen[0] and "a = b" not in seen[0]


WIDE_VOCAB = {
    "course": "数学", "language": "ja",
    "kinds": [
        {"name": "人物", "definition": "人", "examples": ["甲野"], "person": True, "cited_work": False},
        {"name": "モデル", "definition": "m", "examples": ["線形モデル"], "person": False, "cited_work": False},
        {"name": "統計", "definition": "s", "examples": ["平均"], "person": False, "cited_work": False},
        {"name": "手法", "definition": "t", "examples": ["回帰"], "person": False, "cited_work": False},
        {"name": "文献", "definition": "本", "examples": ["本"], "person": False, "cited_work": True},
    ],
    "relations": [
        {"label": "導出", "definition": "条件からモデルを導く", "example": "A → B",
         "from_kinds": ["モデル", "統計"], "to_kinds": ["モデル"]},
        {"label": "提唱", "definition": "人物が唱えた", "example": "A → B",
         "from_kinds": ["人物"], "to_kinds": ["モデル", "手法"]},
        {"label": "出典", "definition": "本に書かれている", "example": "A → B",
         "from_kinds": ["文献"], "to_kinds": ["手法"]},
        {"label": OTHER_LABEL, "definition": "", "example": ""},
    ],
}


def test_concept_labels_allow_all_content_kinds_on_both_ends(monkeypatch):
    lectures = [lec(1, "回帰から平均を導く。甲野は平均を唱えた。")]
    answers = {(1, 1, 1): ([c("kaiki", "回帰", "手法", 1), c("heikin", "平均", "統計", 1), c("kouno", "甲野", "人物", 1)],
                           [rel("回帰", "平均", 1, "回帰から平均を導く", label="導出"),
                            rel("甲野", "平均", 1, "甲野は平均を唱えた", label="提唱")])}
    _, relations, info, dropped, _, _ = run(monkeypatch, lectures, answers, size=1, vocab=WIDE_VOCAB)
    by = {(r["from"], r["to"]): r["label"] for r in relations}
    assert by[("kaiki", "heikin")] == "導出"                # 概念系のラベル: 両端とも概念系の種類なら残す（向きはそのまま）
    assert by[("kouno", "heikin")] == OTHER_LABEL           # 人物のラベルは広げない（#98 の規則のまま）


def test_concept_label_kinds_rule():
    from quizmap.vocab import concept_label_kinds
    assert concept_label_kinds(WIDE_VOCAB) == {"導出": ["モデル", "統計", "手法"]}
    old = {"kinds": [{"name": "a"}, {"name": "b"}],
           "relations": [{"label": "x", "from_kinds": ["a"], "to_kinds": ["b"]}]}
    assert concept_label_kinds(old) == {}                   # 旗の無い古い vocab では広げない


def test_direction_text_mentions_wide_kinds(monkeypatch):
    seen = []
    monkeypatch.setattr(extraction, "call_json",
                        lambda client, **kw: seen.append(kw["user"]) or {"concepts": [], "relations": []})
    extraction.extract_graph(None, "m", "数学", WIDE_VOCAB, [lec(1, "x")], log=lambda *_: None,
                             pages_per_call=1, overlap=0, chunk_chars=0)
    assert "【向き: from は モデル・統計、to は モデル。両端とも モデル・統計・手法 のどれでもよい（向きは定義に従う）】" in seen[0]
    assert "【向き: from は 人物、to は モデル・手法】" in seen[0]


# ---------------------------------------------------------------- 関係の端の名前（#119 のつづき）

def test_merge_key_ignores_english_plural_article_hyphen_and_apostrophe():
    mk = extraction.merge_key
    assert mk("singular points") == mk("singular point") == mk("Singular Points")
    assert mk("Eigenvalues") == mk("eigenvalue")
    assert mk("system of linear equations") == mk("system of linear equation")
    assert mk("A Predator–Prey System") == mk("predator-prey system") == mk("Predator Prey System")
    assert mk("Student’s t-Distribution") == mk("Students t distribution")
    assert mk("statistics") != mk("statistic")                          # ics は複数形の s として除かない
    assert mk("analysis") == "analysis" and mk("class") == "class"
    assert mk("A") != mk("a")                                           # 1 語だけの冠詞らしい語は除かない・記号は区別
    assert mk("幸福(エウダイモニアー)") == mk("幸福")                    # 日本語は今までどおり


def test_relation_ends_written_as_ids_are_resolved(monkeypatch):
    """LLM が関係の端に label ではなく id（snake_case）を書いても、そのかたまりの概念につなぐ（#119）。"""
    lectures = [lec(1, "Kono proposed the Otsu theory.", "Otsu theory again. Kono.")]
    answers = {
        (1, 1, 1): ([c("kono", "Kono", "人物", 1), c("otsu_theory", "Otsu theory", "学説", 1)],
                    [rel("kono", "otsu_theory", 1, "Kono proposed the Otsu theory")]),
        (1, 2, 2): ([], [rel("kono", "otsu_theory", 2, "Otsu theory again. Kono")]),   # ほかのかたまりの id、本文に名前あり
    }
    _, relations, info, dropped, _, _ = run(monkeypatch, lectures, answers, size=1)
    assert [(r["from"], r["to"], r["source"]["page"]) for r in relations] == [("kono", "otsu_theory", 1)]
    assert "存在しない概念" not in dropped and dropped["重複"] == 1


def test_id_from_other_chunk_needs_name_in_this_chunk(monkeypatch):
    lectures = [lec(1, "Kono proposed the Otsu theory.", "Nothing here about them.")]
    answers = {
        (1, 1, 1): ([c("kono", "Kono", "人物", 1), c("otsu_theory", "Otsu theory", "学説", 1)], []),
        (1, 2, 2): ([], [rel("kono", "otsu_theory", 2, "Nothing here about them")]),
    }
    _, relations, _, dropped, _, _ = run(monkeypatch, lectures, answers, size=1)
    assert relations == [] and dropped["存在しない概念"] == 1


def test_plural_variants_merge_into_one_concept(monkeypatch):
    lectures = [lec(1, "boundary points are checked.", "a boundary point here.", "boundary point again.")]
    answers = {
        (1, 1, 1): ([c("boundary_points", "boundary points", "学説", 1)], []),
        (1, 2, 2): ([c("boundary_point", "boundary point", "学説", 2)], []),
        (1, 3, 3): ([c("boundary_point", "boundary point", "学説", 3)], []),
    }
    concepts, *_ = run(monkeypatch, lectures, answers, size=1)
    assert labels(concepts) == ["boundary point"]                       # 多く挙がった表記
    (bp,) = concepts.values()
    assert bp["id"] == "boundary_points" and [s["page"] for s in bp["sources"]] == [1, 2, 3]


def test_prompt_says_relation_ends_are_labels_not_ids():
    assert "label をそのまま書く（id ではなく label）" in extraction.EXTRACT_USER


# ---------------------------------------------------------------- 同じ人物の別の書き方（姓だけ・頭文字）

@pytest.mark.parametrize("a, b, same", [
    ("カント", "イマヌエル・カント", True),
    ("ヘーゲル", "ゲオルク・ヴィルヘルム・フリードリヒ・ヘーゲル", True),
    ("フロム", "エーリヒ＝フロム", True),
    ("ジョン・ミル", "ジェームズ・ミル", False),                      # 同じ姓で名が違う
    ("L.M.Sullivan", "Lisa M. Sullivan", True),
    ("Sullivan", "Lisa M. Sullivan", True),
    ("L. Sullivan", "K. Sullivan", False),                            # 頭文字が違う
    ("Lisa Sullivan", "Laura Sullivan", False),                       # 頭文字は同じでも名が違う
    ("Dr. Alan H. Howard", "A. Howard", True),                        # 敬称は除く
    ("Kant", "カント", False),                                        # 文字の種類が違えばまとめない
])
def test_same_person(a, b, same):
    assert extraction._same_person(extraction._person_name(a), extraction._person_name(b)) is same


def P(cid, label, kind="人物"):
    return {"id": cid, "label": label, "kind": kind}


def test_person_aliases_skip_ambiguous_surname_and_non_person_kinds():
    concepts = {x["id"]: x for x in [P("kant", "カント"), P("ikant", "イマヌエル・カント"),
                                     P("mill", "ミル"), P("jmill", "ジョン・ミル"), P("jamill", "ジェームズ・ミル"),
                                     P("hegel", "ヘーゲル", kind="学説"), P("ghegel", "G・ヘーゲル")]}
    alias = extraction.person_aliases(concepts, {"人物"}, {"kant": 1, "ikant": 3})
    assert alias == {"kant": "ikant"}                 # ミルは 2 人に合うのでまとめない、学説の種類は見ない


def test_extract_graph_merges_people_and_their_relations(monkeypatch):
    lectures = [lec(1, "イマヌエル・カントは乙説を唱えた。", "カントの乙説。カント", "カントは丙説を唱えた。")]
    answers = {
        (1, 1, 1): ([c("ikant", "イマヌエル・カント", "人物", 1), c("otsu", "乙説", "学説", 1)],
                    [rel("イマヌエル・カント", "乙説", 1, "イマヌエル・カントは乙説を唱えた")]),
        (1, 2, 2): ([c("kant", "カント", "人物", 2)], []),
        (1, 3, 3): ([c("kant", "カント", "人物", 3), c("hei", "丙説", "学説", 3)],
                    [rel("カント", "丙説", 3, "カントは丙説を唱えた")]),
    }
    concepts, relations, info, *_ = run(monkeypatch, lectures, answers, size=1)
    assert labels(concepts) == ["カント", "丙説", "乙説"]            # 多く挙がった表記
    (k,) = [x for x in concepts.values() if x["kind"] == "人物"]
    assert k["id"] == "kant" and [s["page"] for s in k["sources"]] == [1, 2, 3]   # id も多く挙がったほう
    assert sorted((r["from"], r["to"]) for r in relations) == [("kant", "hei"), ("kant", "otsu")]
    assert info["merged_people"] == [["イマヌエル・カント", "カント"]]


@pytest.mark.parametrize("a, b, same", [
    ("G. W. ヘーゲル", "ゲオルク・ヴィルヘルム・フリードリヒ・ヘーゲル", True),    # 頭文字と片仮名の名
    ("R. G. コリングウッド", "ロビン・ジョージ・コリングウッド", True),
    ("G. E. ムーア", "ジョージ・エドワード・ムーア", True),
    ("G. W. ヘーゲル", "ヨハン・ヘーゲル", False),                                  # G と ヨ は合わない
    ("ジョン・ミル", "ジョン・スチュアート・ミル", True),
])
def test_same_person_initials_and_katakana(a, b, same):
    assert extraction._same_person(extraction._person_name(a), extraction._person_name(b)) is same


def test_surname_only_joins_after_full_names_are_merged():
    concepts = {x["id"]: x for x in [P("g1", "ゲオルク・ヴィルヘルム・フリードリヒ・ヘーゲル"), P("g2", "ヘーゲル"),
                                     P("g3", "G. W. ヘーゲル"), P("mill", "ミル"), P("jm", "ジョン・ミル"),
                                     P("jam", "ジェームズ・ミル")]}
    alias = extraction.person_aliases(concepts, {"人物"}, {"g2": 5})
    assert alias == {"g1": "g2", "g3": "g2"}          # 3 つが 1 つに。ミルは別人 2 人なのでまとめない


def test_title_in_double_brackets_is_not_a_person(monkeypatch):
    lectures = [lec(1, "エンデの『モモ』を読む。")]
    answers = {(1, 1, 1): ([c("momo", "『モモ』", "人物", 1), c("ende", "エンデ", "人物", 1)], [])}
    concepts, _, info, *_ = run(monkeypatch, lectures, answers, size=1)
    kinds = {x["label"]: x["kind"] for x in concepts.values()}
    assert kinds == {"『モモ』": OTHER_LABEL, "エンデ": "人物"}
    assert info["titles_not_person"] == ["『モモ』"]


def test_kind_dominance():
    import collections
    d = extraction.kind_dominance(collections.Counter({"統計": 128, "文脈": 37, "モデル": 22, OTHER_LABEL: 50}))
    assert d == {"kind": "統計", "share": 0.6845, "warning": True}
    assert extraction.kind_dominance(collections.Counter({"a": 5, "b": 5}))["warning"] is False
    assert extraction.kind_dominance(collections.Counter())["warning"] is False


def test_vocab_prompt_asks_for_kinds_by_role_and_wide_contrast_ends():
    from quizmap import vocab
    assert "1 つの種類に概念の大半が入らないようにする" in vocab.USER_TEMPLATE
    assert "法律・制度・社会の規範の種類に入れず" in vocab.USER_TEMPLATE
    assert "対立・対比のラベルには、資料で対比される両端の種類をすべて含めること" in vocab.USER_TEMPLATE


def test_kanji_surname_prefix_merges_when_unique():
    concepts = {x["id"]: x for x in [P("m1", "宮坂道夫"), P("m2", "宮坂"), P("t1", "高橋和巳"), P("t2", "高橋"),
                                     P("t3", "高橋源一郎"), P("w", "和辻哲郎"), P("k", "金子"), P("k2", "金子武蔵", kind="学説")]}
    alias = extraction.person_aliases(concepts, {"人物"}, {"m1": 3, "m2": 1})
    assert alias == {"m2": "m1"}                     # 高橋は 2 人に合うのでまとめない。種類の違う金子武蔵とは合わせない
