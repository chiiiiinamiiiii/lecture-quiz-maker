"""取り込みへの RAG の組み込み（docs/rag-design.md、#78）。API は呼ばない。

quizmap.rag（RAG 担当）と extraction.complete_relations（プロンプト改善担当）は偽物を差し込む。
確かめること:
- 段階の順番: read → vocab → index → concepts → relations → completion → slides。進捗は減らない
- 索引は抽出の前に pages.json と同じ形のページで作って保存し、補完は抽出の後・スライド画像の前
- 補完の結果が graph.json になる
- 索引・補完の失敗では取り込みは止まらず、抽出だけの地図が残る
- QUIZMAP_RAG=off なら今まで通り（rag を使わない）
- 補完の前の graph を graph_before_completion.json に残す
- open_rag: 索引と Wiki を開く。言語は vocab の meta.language
- 本物の quizmap.rag（埋め込みだけ差し替え）でも、取り込み → open_rag がつながる
"""
from __future__ import annotations

import sys
import types

import pytest

from quizmap import courses, extraction, vocab
from quizmap.common import Lecture, read_json, write_json

VOCAB = {"course": "x", "kinds": [{"name": "人物"}], "relations": [{"label": "提唱"}],
         "meta": {"language": "en"}}


@pytest.fixture
def rag_fakes(monkeypatch, courses_dir):
    """PDF・LLM・rag・抽出・補完・スライド画像を差し替え、呼ばれた順を events に記録する。"""
    ev: dict = {"order": [], "progress": [], "build_pages": None, "wiki_lang": None,
                "complete_args": None, "slides_kw": None, "fail": set()}

    def load_lecture_files(pairs):
        return [Lecture(number=n, path=p, pages=[f"L{n} page one", f"L{n} page two"])
                for p, n in sorted(pairs, key=lambda x: x[1])]

    class FakeIndex:
        loaded: dict = {}

        @classmethod
        def build(cls, client, pages):
            ev["order"].append("build")
            if "build" in ev["fail"]:
                raise RuntimeError("embed 429")
            ev["build_pages"] = pages
            return cls()

        def save(self, course_dir):
            ev["order"].append("save")
            write_json(course_dir / "rag_index.json", {"fake": True})

        @classmethod
        def load(cls, course_dir):
            p = course_dir / "rag_index.json"
            return cls() if p.exists() else None

    class FakeWiki:
        def __init__(self, course_dir, lang):
            ev["wiki_lang"] = lang
            self.course_dir, self.lang = course_dir, lang

    fake_rag = types.ModuleType("quizmap.rag")
    fake_rag.SlideIndex, fake_rag.Wiki = FakeIndex, FakeWiki
    monkeypatch.setitem(sys.modules, "quizmap.rag", fake_rag)
    import quizmap
    monkeypatch.setattr(quizmap, "rag", fake_rag, raising=False)

    def run_extraction(client, model, vocab_, lectures, out_dir, slides=True, log=print,
                       step=lambda n: None, **kw):
        ev["order"].append("extract")
        ev["slides_kw"] = slides
        step("concepts")
        for lec in lectures:
            log(f"概念 第{lec.number}回: 3")
        step("relations")
        for lec in lectures:
            log(f"関係 第{lec.number}回: 2")
        if slides:
            step("slides")
            ev["order"].append("render_in_extract")
        graph = {"course": "x", "concepts": [{"id": "a"}, {"id": "b"}],
                 "relations": [{"from": "a", "to": "b", "label": "提唱"}], "stats": {}}
        write_json(out_dir / "graph.json", graph)
        return graph

    def complete_relations(client, model, vocab_, graph, pages, index, wiki, log=print):
        ev["order"].append("complete")
        ev["complete_args"] = (graph, pages, index, wiki)
        if "complete" in ev["fail"]:
            raise RuntimeError("LLM error")
        g = dict(graph, relations=graph["relations"] + [
            {"from": "b", "to": "a", "label": "提唱", "origin": "external"}])
        g["stats"] = {"completion": {"candidates": 1, "slide": 0, "external": 1}}
        log("補完: 1 本")
        return g

    def render_slides(lectures, slides_dir, dpi):
        ev["order"].append("render")
        return 4

    monkeypatch.delenv("QUIZMAP_RAG", raising=False)
    monkeypatch.setattr(courses, "load_lecture_files", load_lecture_files)
    monkeypatch.setattr(courses, "make_client", lambda: object())
    monkeypatch.setattr(vocab, "derive_vocab", lambda *a, **k: dict(VOCAB))
    monkeypatch.setattr(extraction, "run_extraction", run_extraction)
    monkeypatch.setattr(extraction, "complete_relations", complete_relations, raising=False)
    monkeypatch.setattr(extraction, "render_slides", render_slides)
    return ev


def _course():
    d = courses.create_course("Natural Language Processing")
    info = courses.course_info(d)
    info["files"] = [{"name": "w1.pdf", "lecture": 1, "use": True}, {"name": "w2.pdf", "lecture": 2, "use": True}]
    courses.save_course_info(d, info)
    return d


class _Recorder(courses.Job):
    """段階と進捗を記録する Job。"""

    def save(self):
        self.__dict__.setdefault("_trace", []).append((self.phase, self.progress))
        super().save()


def _run(d):
    job = _Recorder(course_dir=str(d))
    courses._run(job, d, False, "fake-model")
    return job


def _phases(job):
    out = []
    for ph, _ in job._trace:
        if not out or out[-1] != ph:
            out.append(ph)
    return out


# ------------------------------------------------------------------ RAG あり（既定）

def test_phases_in_order_and_progress_never_decreases(rag_fakes):
    d = _course()
    job = _run(d)
    assert job.status == "done", job.error
    assert _phases(job) == ["read", "vocab", "index", "concepts", "relations", "completion", "slides"]
    assert rag_fakes["order"] == ["build", "save", "extract", "complete", "render"]
    progress = [p for _, p in job._trace]
    assert progress == sorted(progress)
    assert job.progress == 1.0


def test_phase_widths_sum_to_one_and_every_phase_has_label():
    assert sum(w for _, _, w in courses.PHASES) == pytest.approx(1.0)
    assert [k for k, _, _ in courses.PHASES] == ["read", "vocab", "index", "concepts", "relations",
                                                  "completion", "slides"]
    assert all(courses.PHASE_LABEL[k] for k, _, _ in courses.PHASES)


def test_index_built_from_pages_and_saved(rag_fakes):
    d = _course()
    _run(d)
    assert rag_fakes["build_pages"] == [
        {"lecture": 1, "page": 1, "text": "L1 page one"}, {"lecture": 1, "page": 2, "text": "L1 page two"},
        {"lecture": 2, "page": 1, "text": "L2 page one"}, {"lecture": 2, "page": 2, "text": "L2 page two"},
    ]
    assert (d / "rag_index.json").exists()


def test_completion_gets_extracted_graph_and_result_is_saved(rag_fakes):
    d = _course()
    _run(d)
    graph, pages, index, wiki = rag_fakes["complete_args"]
    assert [r.get("origin") for r in graph["relations"]] == [None]       # 抽出の結果がそのまま渡る
    assert pages == rag_fakes["build_pages"]
    assert index is not None
    assert wiki.lang == "en" and wiki.course_dir == d                     # vocab の meta.language
    saved = read_json(d / "graph.json")
    assert [r.get("origin") for r in saved["relations"]] == [None, "external"]
    assert saved["stats"]["completion"]["external"] == 1
    assert saved["course"] == "Natural Language Processing"               # 授業名は利用者のもの
    assert rag_fakes["slides_kw"] is False                                # スライド画像は補完の後に作る
    before = read_json(d / "graph_before_completion.json")
    assert [r.get("origin") for r in before["relations"]] == [None]       # 補完の前の graph


def test_index_failure_skips_completion_but_ingest_finishes(rag_fakes):
    rag_fakes["fail"].add("build")
    d = _course()
    job = _run(d)
    assert job.status == "done"
    assert rag_fakes["order"] == ["build", "extract", "render"]
    assert not (d / "rag_index.json").exists()
    assert any("索引を作れなかった" in m for m in job.log)
    assert len(read_json(d / "graph.json")["relations"]) == 1
    assert read_json(d / "graph_before_completion.json")["relations"] == read_json(d / "graph.json")["relations"]


def test_completion_failure_keeps_extracted_graph(rag_fakes):
    rag_fakes["fail"].add("complete")
    d = _course()
    job = _run(d)
    assert job.status == "done"
    assert rag_fakes["order"] == ["build", "save", "extract", "complete", "render"]
    assert any("関係の補完に失敗した" in m for m in job.log)
    assert len(read_json(d / "graph.json")["relations"]) == 1


# ------------------------------------------------------------------ RAG なし

@pytest.mark.parametrize("value", ["off", "OFF", "0", "false", "no"])
def test_rag_off_is_previous_behaviour(rag_fakes, monkeypatch, value):
    monkeypatch.setenv("QUIZMAP_RAG", value)
    d = _course()
    job = _run(d)
    assert job.status == "done"
    assert rag_fakes["order"] == ["extract", "render_in_extract"]       # 索引・補完なし、画像は抽出の中
    assert rag_fakes["slides_kw"] is True
    assert "index" not in _phases(job) and "completion" not in _phases(job)
    assert not (d / "rag_index.json").exists()
    assert not (d / "graph_before_completion.json").exists()


@pytest.mark.parametrize("value,expected", [(None, True), ("on", True), ("1", True), ("off", False)])
def test_rag_enabled(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("QUIZMAP_RAG", raising=False)
    else:
        monkeypatch.setenv("QUIZMAP_RAG", value)
    assert courses.rag_enabled() is expected


# ------------------------------------------------------------------ open_rag

def test_open_rag_after_ingest(rag_fakes):
    d = _course()
    _run(d)
    index, wiki = courses.open_rag(d)
    assert index is not None
    assert wiki.lang == "en" and wiki.course_dir == d


def test_open_rag_without_index(rag_fakes):
    d = _course()
    write_json(d / "vocab.json", {"meta": {"language": "ja"}})
    index, wiki = courses.open_rag(d)
    assert index is None
    assert wiki.lang == "ja"


def test_open_rag_old_vocab_uses_page_text(rag_fakes):
    """meta.language が無い（#26 より前の）vocab なら、pages.json の本文から決める。"""
    d = _course()
    write_json(d / "vocab.json", {"kinds": []})
    write_json(d / "pages.json", [{"lecture": 1, "page": 1, "text": "Attention is all you need"}])
    assert courses.open_rag(d)[1].lang == "en"


def test_open_rag_off(rag_fakes, monkeypatch):
    monkeypatch.setenv("QUIZMAP_RAG", "off")
    d = _course()
    assert courses.open_rag(d) == (None, None)


# ------------------------------------------------------------------ 本物の quizmap.rag

@pytest.fixture
def real_rag(rag_fakes, monkeypatch):
    """偽の rag を外し、本物の quizmap.rag を使う。埋め込み（HTTP）だけ差し替える。"""
    import importlib
    import quizmap

    monkeypatch.delitem(sys.modules, "quizmap.rag", raising=False)
    rag = importlib.import_module("quizmap.rag")
    monkeypatch.setattr(quizmap, "rag", rag, raising=False)

    def fake_embed(client, texts, task="RETRIEVAL_DOCUMENT"):
        return [[float(len(t) % 7 + 1), float(i + 1), 1.0] for i, t in enumerate(texts)]

    monkeypatch.setattr(rag, "embed", fake_embed)
    return rag


def test_real_rag_ingest_then_open(real_rag, rag_fakes):
    d = _course()
    job = _run(d)
    assert job.status == "done", job.log
    assert rag_fakes["order"] == ["extract", "complete", "render"]
    index, wiki = courses.open_rag(d)
    assert isinstance(index, real_rag.SlideIndex) and len(index) == 4
    assert [(p["lecture"], p["page"]) for p in index.pages] == [(1, 1), (1, 2), (2, 1), (2, 2)]
    assert isinstance(wiki, real_rag.Wiki) and wiki.lang == "en"
    _, _, idx, w = rag_fakes["complete_args"]
    assert isinstance(idx, real_rag.SlideIndex) and isinstance(w, real_rag.Wiki)


def test_real_rag_broken_index_opens_as_none(real_rag, rag_fakes):
    d = _course()
    _run(d)
    (d / "rag_index.json").write_text("{\"model\": \"gemini-embedding-001\"}", encoding="utf-8")
    index, wiki = courses.open_rag(d)
    assert index is None and wiki is not None


def test_real_rag_other_embed_model_opens_as_none(real_rag, rag_fakes):
    d = _course()
    _run(d)
    data = read_json(d / "rag_index.json")
    data["model"] = "some-other-embedding"
    write_json(d / "rag_index.json", data)
    assert courses.open_rag(d)[0] is None
