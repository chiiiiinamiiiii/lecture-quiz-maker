"""quizmap.courses._run（取り込みの裏処理）。LLM 呼び出しと PDF の読み込みを差し替え、API は呼ばない。

確かめること: 成功・失敗・使う資料ゼロ の 3 通りで、Job の状態と job.json が正しくなるか。
"""
from __future__ import annotations

import time

import pytest

from quizmap import common, courses, extraction, vocab
from quizmap.common import Lecture, read_json, write_json

VOCAB = {"course": "LLM が付けた名前", "kinds": [{"name": "人物"}], "relations": [{"label": "提唱"}]}


@pytest.fixture
def fakes(monkeypatch):
    """LLM と PDF を差し替える。calls に呼ばれ方を記録する。"""
    calls = {"derive": 0, "extract": 0, "lectures": None, "vocab_course": None}

    def load_lecture_files(pairs):
        calls["lectures"] = sorted(n for _, n in pairs)
        return [Lecture(number=n, path=p, pages=["p1", "p2"]) for p, n in sorted(pairs, key=lambda x: x[1])]

    def derive_vocab(client, model, lectures, course_hint, log=print):
        calls["derive"] += 1
        log("語彙を導出した")
        return dict(VOCAB)

    def run_extraction(client, model, vocab_, lectures, out_dir, log=print, step=lambda n: None, **kw):
        calls["extract"] += 1
        calls["vocab_course"] = vocab_["course"]
        step("concepts")
        for lec in lectures:
            log(f"概念 第{lec.number}回: 3")
        step("relations")
        for lec in lectures:
            log(f"関係 第{lec.number}回: 2")
        step("slides")
        graph = {"course": vocab_["course"], "concepts": [], "relations": [],
                 "stats": {"lectures": [l.number for l in lectures]}}
        write_json(out_dir / "graph.json", graph)
        return graph

    monkeypatch.setenv("QUIZMAP_RAG", "off")      # RAG を入れたときは test_ingest_rag.py
    monkeypatch.setattr(courses, "load_lecture_files", load_lecture_files)
    monkeypatch.setattr(courses, "make_client", lambda: object())
    monkeypatch.setattr(vocab, "derive_vocab", derive_vocab)
    monkeypatch.setattr(extraction, "run_extraction", run_extraction)
    return calls


def _course(name="倫理学の世界", files=None):
    d = courses.create_course(name)
    info = courses.course_info(d)
    info["files"] = files if files is not None else [
        {"name": "第2回.pdf", "lecture": 2, "use": True},
        {"name": "第1回.pdf", "lecture": 1, "use": True},
        {"name": "読書課題.pdf", "lecture": None, "use": False},
    ]
    courses.save_course_info(d, info)
    return d


def _run(d, rebuild_vocab=False):
    job = courses.Job(course_dir=str(d))
    courses._run(job, d, rebuild_vocab, "fake-model")
    return job


def _assert_saved(d, job):
    saved = read_json(d / "job.json")
    assert saved["status"] == job.status
    assert saved["error"] == job.error
    assert saved["finished_at"] == job.finished_at != ""
    assert saved["log"] == job.log


# ------------------------------------------------------------------ 成功

def test_success(courses_dir, fakes):
    d = _course()
    job = _run(d)
    assert job.status == "done"
    assert job.progress == 1.0
    assert job.error == ""
    assert job.n_lectures == 2
    assert job.done_concepts == 2 and job.done_relations == 2
    assert job.log[-1].startswith("完了（全体 ")
    assert fakes["lectures"] == [1, 2]                      # use=False は渡さない
    assert fakes["derive"] == 1
    assert fakes["vocab_course"] == "倫理学の世界"          # 授業名は利用者が入れたもの（§16.3）
    assert (d / "vocab.json").exists()
    assert read_json(d / "graph.json")["course"] == "倫理学の世界"
    assert courses.list_courses()[0]["ready"] is True
    _assert_saved(d, job)
    assert courses.current_job(d).status == "done"


def test_success_reuses_existing_vocab(courses_dir, fakes):
    d = _course()
    write_json(d / "vocab.json", {**VOCAB, "kinds": [{"name": "既存"}]})
    job = _run(d)
    assert job.status == "done"
    assert fakes["derive"] == 0
    assert "既存の語彙（vocab.json）を使う" in job.log
    assert read_json(d / "vocab.json")["kinds"] == [{"name": "既存"}]


def test_success_rebuild_vocab(courses_dir, fakes):
    d = _course()
    write_json(d / "vocab.json", {**VOCAB, "kinds": [{"name": "既存"}]})
    job = _run(d, rebuild_vocab=True)
    assert job.status == "done"
    assert fakes["derive"] == 1
    assert read_json(d / "vocab.json")["kinds"] == VOCAB["kinds"]


def test_progress_never_goes_back(courses_dir, fakes, monkeypatch):
    d = _course()
    seen = []
    orig = courses.Job.save

    def save(self):
        seen.append(self.progress)
        orig(self)

    monkeypatch.setattr(courses.Job, "save", save)
    _run(d)
    assert seen == sorted(seen)
    assert seen[-1] == 1.0


# ------------------------------------------------------------------ 失敗

def test_failure_in_extraction(courses_dir, fakes, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("429 が続いた")

    monkeypatch.setattr(extraction, "run_extraction", boom)
    d = _course()
    job = _run(d)
    assert job.status == "error"
    assert job.error == "RuntimeError: 429 が続いた"
    assert any(line.startswith("失敗: RuntimeError") for line in job.log)
    assert not (d / "graph.json").exists()
    _assert_saved(d, job)
    assert courses.current_job(d).status == "error"


def test_failure_in_vocab(courses_dir, fakes, monkeypatch):
    def boom(*a, **kw):
        raise ValueError("JSON が壊れていた")

    monkeypatch.setattr(vocab, "derive_vocab", boom)
    d = _course()
    job = _run(d)
    assert job.status == "error"
    assert "ValueError" in job.error
    assert not (d / "vocab.json").exists()
    assert fakes["extract"] == 0
    _assert_saved(d, job)


def test_failure_missing_api_key(courses_dir, fakes, monkeypatch):
    # 本物の make_client を使う。.env を読まないようにして、キーが無い状況を作る（API は呼ばない）
    monkeypatch.setattr(courses, "make_client", common.make_client)
    monkeypatch.setattr(common, "load_dotenv", lambda *a, **kw: None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    d = _course()
    job = _run(d)                                         # 例外が外に漏れないこと（#2）
    assert job.status == "error"
    assert job.error.startswith("MissingAPIKeyError: GEMINI_API_KEY がない")
    assert fakes["derive"] == 0
    _assert_saved(d, job)
    assert courses.current_job(d).status == "error"


def test_missing_api_key_does_not_block_next_ingest(courses_dir, fakes, monkeypatch, ticking_clock):
    """キーが無くて失敗したあと、キーを入れれば（再起動せずに）次の取り込みを始められる（#2）。"""
    monkeypatch.setattr(courses, "make_client", common.make_client)
    monkeypatch.setattr(common, "load_dotenv", lambda *a, **kw: None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    d = _course()
    job = courses.start_ingest(d, model="fake-model")
    _wait(job)
    assert job.status == "error"
    assert courses.running_job() is None
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-not-used")   # 差し替えた LLM しか呼ばないので API には行かない
    job2 = courses.start_ingest(d, model="fake-model")
    _wait(job2)
    assert job2.status == "done"


# ------------------------------------------------------------------ 使う資料ゼロ

@pytest.mark.parametrize("files", [
    [],
    [{"name": "読書課題.pdf", "lecture": None, "use": False},
     {"name": "第1回.pdf", "lecture": 1, "use": False}],
])
def test_no_files_to_use(courses_dir, fakes, files):
    d = _course(files=files)
    job = _run(d)
    assert job.status == "error"
    assert "使う資料が 1 つもありません" in job.error
    assert fakes["lectures"] is None and fakes["derive"] == 0 and fakes["extract"] == 0
    assert job.progress == 0.0
    _assert_saved(d, job)


# ------------------------------------------------------------------ start_ingest（スレッド経由）

def _wait(job, timeout=5.0):
    end = time.monotonic() + timeout
    while job.status == "running" and time.monotonic() < end:
        time.sleep(0.01)


def test_start_ingest_runs_in_background_and_allows_one_at_a_time(courses_dir, fakes, monkeypatch, ticking_clock):
    import threading
    gate = threading.Event()
    orig = courses.load_lecture_files

    def slow(pairs):
        gate.wait(5)
        return orig(pairs)

    monkeypatch.setattr(courses, "load_lecture_files", slow)
    a, b = _course("A"), _course("B")
    assert a != b
    job = courses.start_ingest(a, model="fake-model")
    assert courses.running_job() is job
    assert read_json(a / "job.json")["status"] == "running"
    with pytest.raises(ValueError):
        courses.start_ingest(b, model="fake-model")
    gate.set()
    _wait(job)
    assert job.status == "done"
    assert courses.running_job() is None
    job_b = courses.start_ingest(b, model="fake-model")    # 終われば次を始められる
    _wait(job_b)
    assert job_b.status == "done"
