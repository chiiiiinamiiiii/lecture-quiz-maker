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
        rag_index.json   RAG のスライド索引（quizmap.rag.SlideIndex。QUIZMAP_RAG=off なら作らない）
        wiki_cache.json  Wikipedia の検索結果のキャッシュ（quizmap.rag.Wiki）
        graph_before_completion.json  関係の補完の前の graph（補完の前後を比べるため。RAG ありのときだけ）

取り込み処理は数分かかり、API 呼び出しも数十回あるので、別スレッドで走らせて進捗を Job に書く。
画面はそれを定期的に読みに行くだけにする（Streamlit の再実行で処理が中断されないように）。
API のレート制限を食い合わないよう、同時に走らせる取り込みは 1 つまで。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path

from quizmap.boilerplate import report_lines, strip_boilerplate
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


def safe_upload_name(filename: str) -> str:
    """アップロードのファイル名を uploads/ の中で使える名前にする。

    パスの部分は捨て、許可しない文字は _ にする。「.」「..」や先頭の「.」（隠しファイル）は使わない（#4）。
    """
    name = _SAFE.sub("_", filename.replace("\\", "/").split("/")[-1])
    name = name.strip().lstrip(".").strip()
    return name or "upload.pdf"


def save_upload(d: Path, filename: str, data: bytes) -> Path:
    """uploads/ に保存して、保存先を返す。

    同じ名前のファイルが既にあれば、中身が同じならそれを使い（書かない）、違えば
    「名前 (2).pdf」のように連番を付けて保存する。既存のファイルは上書きしない（#5）。
    連番は「_2」ではなく「 (2)」にする。「_2」だとファイル名から第2回と読まれてしまう。
    """
    name = safe_upload_name(filename)
    stem, suffix = Path(name).stem, Path(name).suffix
    uploads = d / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    for i in range(1, 1000):
        path = uploads / (name if i == 1 else f"{stem} ({i}){suffix}")
        try:
            with path.open("xb") as f:          # 無ければ作る。あれば FileExistsError（上書きしない）
                f.write(data)
            return path
        except FileExistsError:
            if path.is_file() and path.read_bytes() == data:
                return path
    raise RuntimeError(f"同じ名前のファイルが多すぎる: {name}")


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

PHASES = [  # (名前, 表示, 進捗の幅)。index と completion は RAG（docs/rag-design.md）。off なら飛ばす
    # 索引づくりは概念・関係の抽出と同時に別スレッドで走る（#107）。ここの index は「索引の完了を待つ」段階
    ("read", "PDF の読み込み", 0.05),
    ("vocab", "語彙の導出（§4）", 0.08),
    ("concepts", "概念の抽出（§5）", 0.38),
    ("relations", "関係の抽出（§5）", 0.27),
    ("index", "索引づくり", 0.05),
    ("completion", "関係の補完", 0.12),
    ("slides", "スライド画像の作成", 0.05),
]
PHASE_LABEL = {k: label for k, label, _ in PHASES}


def _phase_span(phase: str) -> tuple[float, float]:
    """その段階の (進捗の始まり, 幅)。"""
    start = 0.0
    for k, _, w in PHASES:
        if k == phase:
            return start, w
        start += w
    return start, 0.0


SLIDE_DPI = 80


def rag_enabled() -> bool:
    """設定 QUIZMAP_RAG（既定 on）。off / 0 / false / no なら RAG を使わない（今までと同じ動き）。"""
    return os.environ.get("QUIZMAP_RAG", "on").strip().lower() not in ("off", "0", "false", "no")


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
        with _job_lock:                    # 索引づくりのスレッドと抽出のスレッドが同時に書く（#107）
            write_json(Path(self.course_dir) / "job.json", asdict(self))

    def set_phase(self, phase: str) -> None:
        self.end_phase()
        self.phase = phase
        self.progress = max(self.progress, _phase_span(phase)[0])
        self.__dict__["_phase_t0"] = time.monotonic()      # job.json には書かない（dataclass の欄ではない）
        self.add(f"── {PHASE_LABEL.get(phase, phase)}")

    def end_phase(self) -> None:
        """いまの段階の所要時間をログに出す（#107）。"""
        t0 = self.__dict__.pop("_phase_t0", None)
        if t0 is not None:
            self.add(f"   {PHASE_LABEL.get(self.phase, self.phase)}: {time.monotonic() - t0:.0f} 秒")

    def add(self, msg: str) -> None:
        with _job_lock:
            self._add(msg)

    def _add(self, msg: str) -> None:
        self.log.append(msg)
        # 回ごとの完了で進捗を細かく進める
        n = max(1, self.n_lectures)
        if msg.startswith("概念 第"):
            self.done_concepts += 1
            start, w = _phase_span("concepts")
            self.progress = max(self.progress, start + w * min(1, self.done_concepts / n))
        elif msg.startswith("関係 第"):
            self.done_relations += 1
            start, w = _phase_span("relations")
            self.progress = max(self.progress, start + w * min(1, self.done_relations / n))
        self.save()


_jobs: dict[str, Job] = {}
_job_lock = threading.RLock()
_lock = threading.Lock()


JOB_READ_RETRIES = 5
JOB_READ_WAIT = 0.05      # 秒


def _read_job_json(path: Path) -> dict | None:
    """job.json を読む。壊れていれば短く待って読み直す（#54）。読めなければ None。

    write_json は置き換えで書くので、ふつうは壊れたファイルは見えない。
    以前の版が書いた途中のファイルや、別のプロセスが直接書いた場合に備える。
    """
    for attempt in range(JOB_READ_RETRIES):
        try:
            data = read_json(path)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        except FileNotFoundError:
            return None
        if attempt < JOB_READ_RETRIES - 1:
            time.sleep(JOB_READ_WAIT)
    return None


def current_job(d: Path) -> Job | None:
    """このコースの取り込み処理の状態。実行中のものが無ければ、前回の記録（job.json）を返す。"""
    job = _jobs.get(str(d))
    if job:
        return job
    path = d / "job.json"
    if path.exists():
        data = _read_job_json(path)
        if data is None:
            return Job(course_dir=str(d), status="error",
                       error="取り込みの記録（job.json）を読めなかった。もう一度実行してください。")
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
    from quizmap import extraction
    from quizmap.vocab import derive_vocab

    t_start = time.monotonic()
    try:
        info = course_info(d)
        pairs = [(d / "uploads" / f["name"], int(f["lecture"])) for f in info["files"] if f.get("use")]
        if not pairs:
            raise ValueError("使う資料が 1 つもありません")
        job.n_lectures = len(pairs)

        job.set_phase("read")
        lectures = load_lecture_files(pairs)
        job.add(f"{len(lectures)} 回 {sum(l.page_count for l in lectures)} ページを読み込んだ")
        boiler = strip_boilerplate(lectures)            # 抽出に渡す本文だけから除く（#119）
        for line in report_lines(boiler):
            job.add(line)
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

        if rag_enabled():
            _extract_with_rag(job, d, client, model, vocab, lectures)
        else:
            job.add("RAG は使わない（QUIZMAP_RAG=off）")
            extraction.run_extraction(client, model, vocab, lectures, d, log=job.add, step=job.set_phase)
        _record_boilerplate(d, boiler)
        rename_course(d, info["name"])
        job.end_phase()
        job.progress = 1.0
        job.status = "done"
        job.add(f"完了（全体 {time.monotonic() - t_start:.0f} 秒）")
    except Exception as e:  # noqa: BLE001  画面に理由を出す
        job.end_phase()
        job.status = "error"
        job.error = error_text(e)
        job.add("失敗: " + job.error)
        _log_traceback(job, e)
    finally:
        job.finished_at = now()
        job.save()


def _record_boilerplate(d: Path, report: dict) -> None:
    """抽出に渡す本文から除いた決まり文句・ページ番号を、graph.json（と補完の前の graph）の stats.boilerplate に残す。"""
    for name in ("graph.json", "graph_before_completion.json"):
        path = d / name
        if path.exists():
            graph = read_json(path)
            graph.setdefault("stats", {})["boilerplate"] = report
            write_json(path, graph)


def quota_reset_jst() -> str:
    """Gemini の 1 日の無料枠が戻る時刻（太平洋時間の 0 時）を、日本時間で「16 時」のように返す。"""
    try:
        from zoneinfo import ZoneInfo

        la = dt.datetime.now(ZoneInfo("America/Los_Angeles"))
        midnight = (la + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return f"{midnight.astimezone(ZoneInfo('Asia/Tokyo')).hour} 時"
    except Exception:  # noqa: BLE001  時刻の表が無い環境
        return "16〜17 時"


def error_text(e: Exception) -> str:
    """画面に出す失敗の理由（1〜2 行）。traceback や API の生の本文は画面に出さず、サーバーのログに出す。

    1 日あたりの無料枠を使い切ったときは、いつ何をすればよいかを書く（#107）。
    """
    from quizmap.common import DailyQuotaError

    if isinstance(e, DailyQuotaError):
        kind = "埋め込み" if "embed" in str(e).lower() else "生成"
        return (f"Gemini の無料枠（{kind}）の 1 日の上限に達しました。日本時間 {quota_reset_jst()}以降に、"
                "もう一度実行してください（済んだ分は残っています）。")
    text = f"{type(e).__name__}: {e}"
    return text if len(text) <= 300 else text[:300] + "…"


def _log_traceback(job: Job, e: Exception) -> None:
    """traceback はサーバーのログ（標準エラー）にだけ出す。画面（job.log）には出さない。"""
    print(f"[error] {job.course_dir}: {type(e).__name__}: {e}", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)


def _pages_of(lectures) -> list[dict]:
    """pages.json と同じ形。"""
    return [{"lecture": lec.number, "page": i, "text": page}
            for lec in lectures for i, page in enumerate(lec.pages, start=1)]


def _extract_with_rag(job: Job, d: Path, client, model: str, vocab: dict, lectures) -> None:
    """（索引づくり ∥ 抽出）→ 関係の補完 → スライド画像（docs/rag-design.md）。

    索引づくりは埋め込みの枠、抽出は生成の枠を使い、互いに待たないので、別スレッドで同時に走らせる（#107）。
    関係の補完は両方が終わってから。
    索引づくりと関係の補完が失敗しても、取り込みは止めずに補完なしの地図を残す（記録には残す）。
    1 日あたりの埋め込みの枠を使い切った 429 は、待たずにすぐ索引なしで進む。
    """
    from quizmap import extraction, rag
    from quizmap.vocab import language_of

    pages = _pages_of(lectures)
    built: dict = {"index": None}

    def build_index() -> None:
        t0 = time.monotonic()
        try:
            index = rag.SlideIndex.build(client, pages, progress=_index_progress(job))
            index.save(d)
            built["index"] = index
            job.add(f"索引: {len(pages)} ページ（{time.monotonic() - t0:.0f} 秒、抽出と同時）")
        except Exception as e:  # noqa: BLE001  補完なしで続ける
            job.add(f"索引を作れなかった（関係の補完は飛ばす）: {error_text(e)}")
            _log_traceback(job, e)

    job.add("索引づくりを、概念・関係の抽出と同時に始める")
    indexer = threading.Thread(target=build_index, name="quizmap-index", daemon=True)
    indexer.start()

    graph = extraction.run_extraction(client, model, vocab, lectures, d, slides=False,
                                      log=job.add, step=job.set_phase)
    write_json(d / "graph_before_completion.json", graph)     # 補完の前後を比べるため（出力評価）

    job.set_phase("index")
    if indexer.is_alive():
        job.add("索引づくりの完了を待つ")
    indexer.join()
    index = built["index"]

    job.set_phase("completion")
    if index is None:
        job.add("索引が無いので関係の補完は飛ばした")
    else:
        try:
            wiki = rag.Wiki(d, language_of(vocab, [p["text"] for p in pages]))
            graph = extraction.complete_relations(client, model, vocab, graph, pages, index, wiki, log=job.add)
            write_json(d / "graph.json", graph)
        except Exception as e:  # noqa: BLE001  抽出だけの地図を残す
            job.add(f"関係の補完に失敗した（抽出だけの地図を使う）: {error_text(e)}")
            _log_traceback(job, e)

    job.set_phase("slides")
    n_imgs = extraction.render_slides(lectures, d / "slides", SLIDE_DPI)
    job.add(f"スライド画像 {n_imgs} 枚")


def start_rag_rebuild(d: Path, model: str = DEFAULT_MODEL) -> Job:
    """取り込み済みのコースに、あとから索引を作り、関係の補完を走らせる（別スレッド）。

    索引の無いコース（RAG より前のもの、索引づくりが途中で失敗したもの）のため。画面のボタンは別に作る。
    補完は graph_before_completion.json（無ければ今の graph.json）から始め直すので、二重には足さない。
    すでに何か走っていれば ValueError。
    """
    for name in ("graph.json", "pages.json", "vocab.json"):
        if not (d / name).exists():
            raise ValueError(f"取り込みが済んでいない（{name} が無い）")
    with _lock:
        if any(j.status == "running" for j in _jobs.values()):
            raise ValueError("別の取り込み処理が実行中です。終わってから始めてください。")
        job = Job(course_dir=str(d), phase="index")
        _jobs[str(d)] = job
    job.save()
    threading.Thread(target=_run_rag_rebuild, args=(job, d, model), daemon=True).start()
    return job


def _run_rag_rebuild(job: Job, d: Path, model: str) -> None:
    from quizmap import extraction, rag
    from quizmap.vocab import language_of

    try:
        pages = read_json(d / "pages.json")
        vocab = read_json(d / "vocab.json")
        vocab["course"] = course_info(d)["name"]
        before = d / "graph_before_completion.json"
        graph = read_json(before if before.exists() else d / "graph.json")
        client = make_client()

        job.set_phase("index")
        index = rag.SlideIndex.build(client, pages, progress=_index_progress(job))
        index.save(d)
        job.add(f"索引: {len(pages)} ページ")

        job.set_phase("completion")
        write_json(before, graph)
        wiki = rag.Wiki(d, language_of(vocab, [p["text"] for p in pages]))
        graph = extraction.complete_relations(client, model, vocab, graph, pages, index, wiki, log=job.add)
        write_json(d / "graph.json", graph)
        rename_course(d, vocab["course"])
        job.progress = 1.0
        job.status = "done"
        job.add("完了")
    except Exception as e:  # noqa: BLE001  画面に理由を出す。graph.json はそのまま残る
        job.status = "error"
        job.error = error_text(e)
        job.add("失敗: " + job.error)
        _log_traceback(job, e)
    finally:
        job.finished_at = now()
        job.save()


def _index_progress(job: Job):
    """索引づくりの進み具合を、バッチごとに job のログに出す（「索引: 40 / 285 ページ」）。

    索引づくりは抽出と同時に走るので、進捗の割合は抽出が進める。索引の段階（抽出の後）にいるときだけ、
    その段階の幅の中で進捗も進める。
    """
    start, width = _phase_span("index")

    def progress(done: int, total: int) -> None:
        if job.phase == "index":
            job.progress = max(job.progress, start + width * (done / total if total else 1.0))
        job.add(f"索引: {done} / {total} ページ")

    return progress


def open_rag(d: Path):
    """出題のときに使う (索引 or None, Wiki or None)。

    索引が無い（RAG より前のコース、索引づくりに失敗した）なら索引は None。
    QUIZMAP_RAG=off なら (None, None)。言語は vocab.json の meta.language（無ければ本文から決める）。
    """
    if not rag_enabled():
        return None, None
    from quizmap import rag
    from quizmap.vocab import language_of

    vocab = read_json(d / "vocab.json") if (d / "vocab.json").exists() else {}
    texts = []
    if not (vocab.get("meta") or {}).get("language") and (d / "pages.json").exists():
        texts = [p["text"] for p in read_json(d / "pages.json")]
    return rag.SlideIndex.load(d), rag.Wiki(d, language_of(vocab, texts))


def assign_lecture_numbers(files: list[dict]) -> list[dict]:
    """入れた資料はすべて使う。講義回は、すでに決まっていて重ならないものはそのまま、
    ファイル名から読めないもの・ほかと重なるものには、いま使っている最大の回の次から順に振る。
    利用者が講義回を割り当てる手順は無い（入れたものを全部活用する）。"""
    out = [dict(f, use=True) for f in files]
    taken: set[int] = set()
    pending = []
    # 同じ回が重なったら、名前の短いほう（「第2回.pdf」と「第2回 補足.pdf」なら前者）がその回を取る
    for f in sorted(out, key=lambda f: (f.get("lecture") is None, f.get("lecture") or 0, len(f["name"]), f["name"])):
        n = f.get("lecture")
        if isinstance(n, int) and n >= 1 and n not in taken:
            taken.add(n)
        else:
            pending.append(f)
    nxt = max(taken, default=0) + 1
    for f in pending:
        f["lecture"] = nxt
        taken.add(nxt)
        nxt += 1
    return sorted(out, key=lambda f: (f["lecture"], f["name"]))


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
