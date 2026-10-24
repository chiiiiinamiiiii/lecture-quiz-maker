"""quizmap.courses: コースの作成・アップロードの保存・講義回の重なり・旧データの取り込み・job.json の読み戻し。"""
from __future__ import annotations

import datetime as real_dt
import json
import types

import pytest

from quizmap import courses
from quizmap.common import read_json, write_json


class _FrozenDatetime(real_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 24, 12, 0, 0)


@pytest.fixture
def frozen_clock(monkeypatch):
    """create_course が同じ秒に 2 回呼ばれる状況を確実に作る。"""
    monkeypatch.setattr(courses, "dt", types.SimpleNamespace(datetime=_FrozenDatetime))


# ------------------------------------------------------------------ create_course

def test_create_course_makes_folder_and_course_json(courses_dir):
    d = courses.create_course("倫理学の世界")
    assert d.parent == courses_dir
    assert (d / "uploads").is_dir()
    info = read_json(d / "course.json")
    assert info["name"] == "倫理学の世界"
    assert info["files"] == []
    assert info["created_at"]
    assert [c["id"] for c in courses.list_courses()] == [d.name]
    assert courses.list_courses()[0]["ready"] is False


def test_create_course_twice_in_same_second_keeps_both(courses_dir, frozen_clock):
    a = courses.create_course("A")
    write_json(a / "course.json", {**read_json(a / "course.json"),
                                   "files": [{"name": "x.pdf", "lecture": 1, "use": True}]})
    b = courses.create_course("B")
    assert a != b
    assert read_json(a / "course.json")["name"] == "A"
    assert len(read_json(a / "course.json")["files"]) == 1
    assert read_json(b / "course.json")["name"] == "B"
    assert {c["name"] for c in courses.list_courses()} == {"A", "B"}


def test_create_course_does_not_reuse_existing_folder(courses_dir, frozen_clock):
    """中身の無いフォルダが同じ id で残っていても、それを使わず別の id にする（#3）。"""
    stale = courses_dir / "c20261024-120000-000000"
    (stale / "uploads").mkdir(parents=True)
    (stale / "uploads" / "残り.pdf").write_bytes(b"old")
    d = courses.create_course("新しい")
    assert d != stale
    assert d.name.startswith("c20261024-120000-000000-")
    assert list((d / "uploads").iterdir()) == []
    assert not (stale / "course.json").exists()


def test_create_course_many_in_same_instant_are_distinct(courses_dir, frozen_clock):
    dirs = [courses.create_course(f"C{i}") for i in range(20)]
    assert len(set(dirs)) == 20
    assert sorted(c["name"] for c in courses.list_courses()) == sorted(f"C{i}" for i in range(20))


def test_create_course_id_format(courses_dir):
    import re
    d = courses.create_course("c")
    assert re.fullmatch(r"c\d{8}-\d{6}-\d{6}", d.name)


# ------------------------------------------------------------------ save_upload

def test_save_upload_keeps_japanese_name(courses_dir):
    d = courses.create_course("c")
    p = courses.save_upload(d, "【配布用】第2回 倫理（前半）.pdf", b"%PDF")
    assert p.parent == d / "uploads"
    assert p.name == "【配布用】第2回 倫理（前半）.pdf"
    assert p.read_bytes() == b"%PDF"


@pytest.mark.parametrize("filename", [
    "../../evil.pdf",
    "/etc/passwd",
    "..\\..\\evil.pdf",
    "sub/dir/第3回.pdf",
    "a:b*c?.pdf",
])
def test_save_upload_stays_inside_uploads(courses_dir, filename):
    d = courses.create_course("c")
    p = courses.save_upload(d, filename, b"x")
    assert p.resolve().parent == (d / "uploads").resolve()
    assert p.is_file()
    assert "/" not in p.name and "\\" not in p.name


def test_save_upload_empty_name_falls_back(courses_dir):
    d = courses.create_course("c")
    assert courses.save_upload(d, "", b"x").name == "upload.pdf"


@pytest.mark.parametrize("filename", ["..", "a/..", ".", "...", " . ", "a\\..", "../.."])
def test_save_upload_dotdot_name(courses_dir, filename):
    d = courses.create_course("c")
    p = courses.save_upload(d, filename, b"x")
    assert p.resolve().parent == (d / "uploads").resolve()
    assert p.is_file()
    assert p.name == "upload.pdf"


@pytest.mark.parametrize("filename, expected", [
    ("../../evil.pdf", "evil.pdf"),
    ("..\\..\\evil.pdf", "evil.pdf"),                       # Windows の区切り
    ("C:\\Users\\me\\第3回.pdf", "第3回.pdf"),
    (".hidden.pdf", "hidden.pdf"),                             # 隠しファイルにしない
    ("..第2回.pdf", "第2回.pdf"),
    ("a:b*c?.pdf", "a_b_c_.pdf"),
    ("  第1回.pdf  ", "第1回.pdf"),
    ("【配布用】第2回 倫理（前半）.pdf", "【配布用】第2回 倫理（前半）.pdf"),
])
def test_safe_upload_name(filename, expected):
    assert courses.safe_upload_name(filename) == expected


def test_save_upload_distinct_names_do_not_collide(courses_dir):
    d = courses.create_course("c")
    a = courses.save_upload(d, "a:b.pdf", b"A")
    b = courses.save_upload(d, "a?b.pdf", b"B")
    c = courses.save_upload(d, "a*b.pdf", b"C")
    assert (a.name, b.name, c.name) == ("a_b.pdf", "a_b (2).pdf", "a_b (3).pdf")
    assert (a.read_bytes(), b.read_bytes(), c.read_bytes()) == (b"A", b"B", b"C")


def test_save_upload_same_content_is_not_duplicated(courses_dir):
    d = courses.create_course("c")
    a = courses.save_upload(d, "第1回.pdf", b"same")
    b = courses.save_upload(d, "第1回.pdf", b"same")
    assert a == b
    assert sorted(p.name for p in (d / "uploads").iterdir()) == ["第1回.pdf"]


def test_save_upload_same_name_new_content_keeps_old(courses_dir):
    d = courses.create_course("c")
    a = courses.save_upload(d, "第1回.pdf", b"old")
    b = courses.save_upload(d, "第1回.pdf", b"new")
    assert a.read_bytes() == b"old"
    assert b.name == "第1回 (2).pdf" and b.read_bytes() == b"new"


@pytest.mark.parametrize("filename, lecture", [
    ("第3回.pdf", 3), ("week3.pdf", 3), ("05_intro.pdf", 5), ("syllabus.pdf", None),
])
def test_numbered_name_keeps_lecture_number(courses_dir, filename, lecture):
    """連番を付けた名前でも、ファイル名から読む講義回は変わらない（「_2」だと第2回と読まれる）。"""
    from quizmap.common import lecture_number_of
    d = courses.create_course("c")
    courses.save_upload(d, filename, b"1")
    p = courses.save_upload(d, filename, b"2")
    assert p.name != filename
    assert lecture_number_of(p) == lecture


def test_save_upload_lecture_number_survives_sanitizing(courses_dir):
    from quizmap.common import lecture_number_of
    d = courses.create_course("c")
    assert lecture_number_of(courses.save_upload(d, "week 3: intro?.pdf", b"x")) == 3


# ------------------------------------------------------------------ lecture_numbers_conflict

def test_conflict_reports_duplicate_lectures_sorted():
    files = [
        {"name": "a", "lecture": 3, "use": True},
        {"name": "b", "lecture": 1, "use": True},
        {"name": "c", "lecture": 3, "use": True},
        {"name": "d", "lecture": 1, "use": True},
        {"name": "e", "lecture": 2, "use": True},
    ]
    assert courses.lecture_numbers_conflict(files) == [1, 3]


def test_conflict_ignores_unused_and_none():
    files = [
        {"name": "a", "lecture": 3, "use": True},
        {"name": "b", "lecture": 3, "use": False},
        {"name": "c", "lecture": None, "use": True},
        {"name": "d", "lecture": None, "use": True},
        {"name": "e", "lecture": 4},
    ]
    assert courses.lecture_numbers_conflict(files) == []
    assert courses.lecture_numbers_conflict([]) == []


# ------------------------------------------------------------------ import_legacy

def _make_legacy(tmp_path):
    src = tmp_path / "legacy"
    (src / "slides").mkdir(parents=True)
    write_json(src / "graph.json", {"course": "旧い授業", "concepts": [], "relations": [],
                                    "stats": {"lectures": [1, 2, 5]}})
    write_json(src / "vocab.json", {"kinds": [], "relations": []})
    write_json(src / "results.json", {"attempts": [], "asked_concepts": []})
    (src / "slides" / "L1_p1.jpg").write_bytes(b"jpg")
    return src


def _snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_import_legacy_copies_without_touching_source(courses_dir, tmp_path):
    src = _make_legacy(tmp_path)
    before = _snapshot(src)
    d = courses.import_legacy(src)
    assert d is not None and d.parent == courses_dir
    assert _snapshot(src) == before                       # 元は消さない・変えない
    for name in ("graph.json", "vocab.json", "results.json", "slides/L1_p1.jpg"):
        assert (d / name).read_bytes() == (src / name).read_bytes()
    info = courses.course_info(d)
    assert info["name"] == "旧い授業"
    assert info["imported_from"] == str(src)
    assert [f["lecture"] for f in info["files"]] == [1, 2, 5]
    assert all(f["use"] for f in info["files"])
    assert courses.list_courses()[0]["ready"] is True


def test_import_legacy_twice_imports_once(courses_dir, tmp_path):
    src = _make_legacy(tmp_path)
    assert courses.import_legacy(src) is not None
    assert courses.import_legacy(src) is None
    assert len(courses.list_courses()) == 1


def test_import_legacy_without_graph_does_nothing(courses_dir, tmp_path):
    src = tmp_path / "empty"
    src.mkdir()
    assert courses.import_legacy(src) is None
    assert courses.list_courses() == []


# ------------------------------------------------------------------ current_job

def test_current_job_none_when_never_run(courses_dir):
    d = courses.create_course("c")
    assert courses.current_job(d) is None


def test_current_job_reports_interrupted_run_as_error(courses_dir):
    d = courses.create_course("c")
    job = courses.Job(course_dir=str(d))
    job.phase = "concepts"
    job.progress = 0.3
    job.save()                                            # status=running のまま、アプリが止まった想定
    got = courses.current_job(d)
    assert got.status == "error"
    assert "途中で終了" in got.error
    assert got.phase == "concepts" and got.progress == 0.3


@pytest.mark.parametrize("status", ["done", "error"])
def test_current_job_returns_finished_record_as_is(courses_dir, status):
    d = courses.create_course("c")
    job = courses.Job(course_dir=str(d), status=status, error="E" if status == "error" else "")
    job.save()
    got = courses.current_job(d)
    assert got.status == status
    assert got.error == job.error


def test_current_job_prefers_live_job_over_file(courses_dir):
    d = courses.create_course("c")
    live = courses.Job(course_dir=str(d))
    courses._jobs[str(d)] = live
    write_json(d / "job.json", {**json.loads(courses.dumps(courses.asdict(live))), "status": "done"})
    assert courses.current_job(d) is live
