"""コース（授業）ごとのデータ置き場と、取り込み処理（語彙の導出 → 抽出）の裏での実行。

置き場:
    data/courses/<id>/
        course.json      コース名・取り込んだファイルと講義回の対応
        uploads/         アップロードされた PDF
        vocab.json       §4 語彙（あれば再利用する。作り直すときだけ消す）
        graph.json       §5 概念と関係
        pages.json       回・ページごとの正規化済みテキスト
        slides/          ページ画像 L{回}_p{ページ}.jpg
        results.json     §8 解答の記録
        job.json         直近の取り込み処理の状態（アプリを再起動しても結果が分かるように）

取り込み処理は数分かかり、API 呼び出しも数十回あるので、別スレッドで走らせて進捗を Job に書く。
画面はそれを定期的に読みに行くだけにする（Streamlit の再実行で処理が中断されないように）。
API のレート制限を食い合わないよう、同時に走らせる取り込みは 1 つまで。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import secrets
import shutil
import threading
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path

from quizmap.common import DATA_DIR, DEFAULT_MODEL, load_lecture_files, make_client, read_json, write_json

COURSES_DIR = DATA_DIR / "courses"


# ------------------------------------------------------------------ コース

def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def list_courses() -> list[dict]:
    """[{id, name, dir, ready}] 新しい順。ready = graph.json がある（地図を開ける）。"""
    out = []
    if COURSES_DIR.exists():
        for d in COURSES_DIR.iterdir():
            meta = d / "course.json"
            if not meta.exists():
                continue
            info = read_json(meta)
            out.append({"id": d.name, "name": info.get("name", d.name), "dir": d,
                        "ready": (d / "graph.json").exists(), "created_at": info.get("created_at", "")})
    return sorted(out, key=lambda c: c["created_at"], reverse=True)


def create_course(name: str) -> Path:
    """新しいコースのフォルダを作る。id は c年月日-時分秒-マイクロ秒。

    同じ id のフォルダが既にあれば作り直さず、末尾に短い乱数を足した別の id にする（#3）。
    フォルダの作成は exist_ok=False なので、同時に呼ばれても同じフォルダを 2 人が使うことはない。
    """
    COURSES_DIR.mkdir(parents=True, exist_ok=True)
    base = dt.datetime.now().strftime("c%Y%m%d-%H%M%S-%f")
    cid = base
    for _ in range(100):
        d = COURSES_DIR / cid
        try:
            d.mkdir()
            break
        except FileExistsError:
            cid = f"{base}-{secrets.token_hex(2)}"
    else:
        raise RuntimeError(f"コースのフォルダを作れなかった: {COURSES_DIR / base}")
    (d / "uploads").mkdir()
    write_json(d / "course.json", {"name": name, "created_at": now(), "files": []})
    return d


def course_info(d: Path) -> dict:
    return read_json(d / "course.json")


def save_course_info(d: Path, info: dict) -> None:
    write_json(d / "course.json", info)


def rename_course(d: Path, name: str) -> None:
    info = course_info(d)
    info["name"] = name
    save_course_info(d, info)
    if (d / "graph.json").exists():
        g = read_json(d / "graph.json")
        g["course"] = name
        write_json(d / "graph.json", g)


_SAFE = re.compile(r"[^\w\-. ()（）【】『』「」]+")


def save_upload(d: Path, filename: str, data: bytes) -> Path:
    name = _SAFE.sub("_", Path(filename).name) or "upload.pdf"
    path = d / "uploads" / name
    path.write_bytes(data)
    return path


def import_legacy(data_dir: Path) -> Path | None:
    """以前の置き場（data/ 直下）にある取り込み済みデータを、コースとしてコピーする。元は消さない。"""
    if not (data_dir / "graph.json").exists():
        return None
    for c in list_courses():
        if course_info(c["dir"]).get("imported_from") == str(data_dir):
            return None
    graph = read_json(data_dir / "graph.json")
    d = create_course(graph.get("course") or "取り込み済みの授業")
    for name in ("vocab.json", "graph.json", "pages.json", "results.json", "quiz_latest.json"):
        if (data_dir / name).exists():
            shutil.copy2(data_dir / name, d / name)
    if (data_dir / "slides").exists():
        shutil.copytree(data_dir / "slides", d / "slides", dirs_exist_ok=True)
    info = course_info(d)
    info["imported_from"] = str(data_dir)
    info["files"] = [{"name": f"第{n}回", "lecture": n, "use": True} for n in graph["stats"]["lectures"]]
    save_course_info(d, info)
    return d


# ------------------------------------------------------------------ 取り込み処理

PHASES = [  # (名前, 表示, 進捗の幅)
    ("read", "PDF の読み込み", 0.05),
    ("vocab", "語彙の導出（§4）", 0.10),
    ("concepts", "概念の抽出（§5）", 0.45),
    ("relations", "関係の抽出（§5）", 0.30),
    ("slides", "スライド画像の作成", 0.10),
]
PHASE_LABEL = {k: label for k, label, _ in PHASES}


@dataclass
class Job:
    course_dir: str
    status: str = "running"          # running / done / error
    phase: str = "read"
    progress: float = 0.0
    log: list[str] = field(default_factory=list)
    error: str = ""
    started_at: str = field(default_factory=now)
    finished_at: str = ""
    n_lectures: int = 0
    done_concepts: int = 0
    done_relations: int = 0

    def save(self) -> None:
        write_json(Path(self.course_dir) / "job.json", asdict(self))

    def set_phase(self, phase: str) -> None:
        self.phase = phase
        start = 0.0
        for k, _, w in PHASES:
            if k == phase:
                break
            start += w
        self.progress = max(self.progress, start)
        self.add(f"── {PHASE_LABEL.get(phase, phase)}")

    def add(self, msg: str) -> None:
        self.log.append(msg)
        # 回ごとの完了で進捗を細かく進める
        n = max(1, self.n_lectures)
        if msg.startswith("概念 第"):
            self.done_concepts += 1
            self.progress = 0.15 + 0.45 * min(1, self.done_concepts / n)
        elif msg.startswith("関係 第"):
            self.done_relations += 1
            self.progress = 0.60 + 0.30 * min(1, self.done_relations / n)
        self.save()


_jobs: dict[str, Job] = {}
_lock = threading.Lock()


def current_job(d: Path) -> Job | None:
    """このコースの取り込み処理の状態。実行中のものが無ければ、前回の記録（job.json）を返す。"""
    job = _jobs.get(str(d))
    if job:
        return job
    path = d / "job.json"
    if path.exists():
        data = read_json(path)
        if data.get("status") == "running":      # アプリが途中で止まった
            data["status"] = "error"
            data["error"] = "アプリが処理の途中で終了した。もう一度実行してください。"
        return Job(**data)
    return None


def running_job() -> Job | None:
    with _lock:
        return next((j for j in _jobs.values() if j.status == "running"), None)


def start_ingest(d: Path, rebuild_vocab: bool = False, model: str = DEFAULT_MODEL) -> Job:
    """取り込み処理を別スレッドで始める。すでに何か走っていれば ValueError。"""
    with _lock:
        if any(j.status == "running" for j in _jobs.values()):
            raise ValueError("別の取り込み処理が実行中です。終わってから始めてください。")
        job = Job(course_dir=str(d))
        _jobs[str(d)] = job
    job.save()
    threading.Thread(target=_run, args=(job, d, rebuild_vocab, model), daemon=True).start()
    return job


def _run(job: Job, d: Path, rebuild_vocab: bool, model: str) -> None:
    from quizmap.extraction import run_extraction
    from quizmap.vocab import derive_vocab

    try:
        info = course_info(d)
        pairs = [(d / "uploads" / f["name"], int(f["lecture"])) for f in info["files"] if f.get("use")]
        if not pairs:
            raise ValueError("使う資料が 1 つもありません")
        job.n_lectures = len(pairs)

        job.set_phase("read")
        lectures = load_lecture_files(pairs)
        job.add(f"{len(lectures)} 回 {sum(l.page_count for l in lectures)} ページを読み込んだ")
        client = make_client()

        job.set_phase("vocab")
        vocab_path = d / "vocab.json"
        if vocab_path.exists() and not rebuild_vocab:
            vocab = read_json(vocab_path)
            job.add("既存の語彙（vocab.json）を使う")
        else:
            vocab = derive_vocab(client, model, lectures, info["name"], log=job.add)
            write_json(vocab_path, vocab)
        vocab["course"] = info["name"]       # 授業名は利用者が入れたものを使う（§16.3）

        run_extraction(client, model, vocab, lectures, d, log=job.add, step=job.set_phase)
        rename_course(d, info["name"])
        job.progress = 1.0
        job.status = "done"
        job.add("完了")
    except Exception as e:  # noqa: BLE001  画面に理由を出す
        job.status = "error"
        job.error = f"{type(e).__name__}: {e}"
        job.add("失敗: " + job.error)
        job.log.append(traceback.format_exc(limit=3))
    finally:
        job.finished_at = now()
        job.save()


def lecture_numbers_conflict(files: list[dict]) -> list[int]:
    seen, dup = set(), set()
    for f in files:
        if not f.get("use"):
            continue
        n = f.get("lecture")
        if n in seen:
            dup.add(n)
        seen.add(n)
    return sorted(x for x in dup if x is not None)


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
