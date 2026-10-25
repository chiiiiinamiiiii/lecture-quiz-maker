"""derive_vocab.py / extract.py で共用する部品。

- PDF フォルダから「第N回」を読み取って講義一覧を作る
- pdftotext でページ単位にテキスト化し、NFKC 正規化と改行除去をかける（仕様 §9 の 1, 2）
- LLM 呼び出し（仕様 §10）。プロバイダ依存はこのファイルに閉じ込める。
  他モジュールは make_client() / call_json() / list_models() だけを使う。
  いまの実装は Gemini API（generativelanguage.googleapis.com/v1beta）を生 HTTP で叩く。
  キーは project/.env の GEMINI_API_KEY、認証は x-goog-api-key ヘッダー。
  呼び出しの間に待機を入れ、429 / 5xx は指数バックオフで再試行する。
"""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
# データの置き場。テストや別の作業ツリーから、本物のデータを触らずに動かすときは環境変数で差し替える
DATA_DIR = Path(os.environ.get("QUIZMAP_DATA_DIR", PROJECT_DIR / "data"))
# API キーを書いた .env の場所。作業ツリーには .env が無いので、本体のものを指せるようにする
ENV_FILE = Path(os.environ.get("QUIZMAP_ENV_FILE", PROJECT_DIR / ".env"))
# 仕様 §10: 無料枠で使える最も安いモデル。環境変数 QUIZMAP_MODEL か --model で切り替えられる。
# 2026-10-19 に /models と料金表で確認: 最安の gemini-2.5-flash-lite は新規ユーザーに提供終了（404）、
# 次に安い gemini-3.1-flash-lite（$0.25 / $1.50 per 1M tokens、無料枠あり）を既定にした
DEFAULT_MODEL = os.environ.get("QUIZMAP_MODEL", "gemini-3.1-flash-lite")
# 無料枠のレート制限対策。生成の呼び出し間隔の下限（秒）。環境変数で調整できる。
# 429 が来たら間隔を 1.5 倍ずつ延ばし（上限 MAX_CALL_INTERVAL）、しばらく 429 が無ければ戻す（#107）
MIN_CALL_INTERVAL = float(os.environ.get("QUIZMAP_MIN_INTERVAL", "4"))
MAX_CALL_INTERVAL = float(os.environ.get("QUIZMAP_MAX_INTERVAL", "12"))
INTERVAL_BACKOFF = 1.5
INTERVAL_RECOVER_AFTER = 60.0   # 秒。この間 429 が無ければ、間隔を 1 段戻す
MAX_RETRIES = 6
OTHER_LABEL = "その他"  # 仕様 §3: 当てはまらない関係のためのラベル。常に 1 つ用意する

# ファイル名から講義回を読む。上から順に試す（「2026A GEH032 … 第10回」の 2026 を拾わないよう、
# 年度などの裸の数字は使わない）
LECTURE_PATTERNS = [
    re.compile(r"第\s*(\d+)\s*[回講]"),
    re.compile(r"(?:week|wk|lecture|lect|lec|class|session|unit|chapter|ch)[\s_\-.]*0*(\d+)", re.I),
    re.compile(r"(?:^|[\s_\-])0*(\d{1,2})(?:[\s_\-.]|$)"),
]


# ---------------------------------------------------------------- env / client

def load_dotenv(path: Path = ENV_FILE) -> None:
    """project/.env の KEY=VALUE を環境変数に入れる（既にある変数は上書きしない）。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


@dataclass
class LLMClient:
    """プロバイダ依存の情報をまとめた薄いクライアント。中身は他モジュールから触らない。"""
    api_key: str
    base_url: str = GEMINI_BASE_URL
    timeout: float = 300.0

    def _headers(self) -> dict:
        return {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}


class MissingAPIKeyError(RuntimeError):
    """GEMINI_API_KEY が設定されていない。CLI は文言を表示して終了コード 1、アプリは画面に理由を出す。"""


def make_client() -> LLMClient:
    """キーが無ければ MissingAPIKeyError。sys.exit はしない（裏のスレッドで呼ぶと Job が止まったまま残るため, #2）。"""
    load_dotenv()
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise MissingAPIKeyError(
            "GEMINI_API_KEY がない。Google AI Studio (https://aistudio.google.com/apikey) で\n"
            "        個人アカウントのキーを作り、次のファイルに書くこと（.gitignore 済み）:\n"
            f"          {PROJECT_DIR / '.env'}  →  GEMINI_API_KEY=AIza..."
        )
    return LLMClient(api_key=key)


# ---------------------------------------------------------------- text

# NFKC で戻らない「CJK 部首補助」(U+2E80-2EFF) の手動対応。倫理学の PDF で実際に出たもの＋よく出るもの
RADICAL_SUPPLEMENT_MAP = str.maketrans({
    "\u2EA0": "民", "\u2EC4": "西", "\u2E92": "巳", "\u2EED": "歯", "\u2ED1": "長",
    "\u2EE9": "黄", "\u2EEB": "斉", "\u2ED8": "青", "\u2EE4": "鬼", "\u2EF2": "亀",
    "\u2EEF": "竜", "\u2EDD": "食", "\u2EE8": "麦", "\u2EDE": "食", "\u2EB1": "罒",
    "\u2E9F": "母", "\u2EBE": "艹", "\u2ECF": "辶", "\u2ECC": "辶", "\u2ED6": "阝",
})


def normalize_text(text: str) -> str:
    """§9-1: NFKC 正規化（康熙部首 → 常用漢字）。§9-2: 改行除去（語の分断を直す）。"""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(RADICAL_SUPPLEMENT_MAP)
    text = text.replace("\r", "").replace("\n", "")
    text = re.sub(r"[ \t　]+", " ", text)
    return text.strip()


@dataclass
class Lecture:
    number: int
    path: Path
    pages: list[str] = field(default_factory=list)  # pages[0] が 1 ページ目（正規化済み）

    @property
    def page_count(self) -> int:
        return len(self.pages)


def lecture_number_of(path: Path) -> int | None:
    name = unicodedata.normalize("NFKC", path.stem)
    for pat in LECTURE_PATTERNS:
        m = pat.search(name)
        if m:
            return int(m.group(1))
    return None


def find_lectures(pdf_dir: Path) -> tuple[list[Lecture], list[Path]]:
    """フォルダ内の PDF を「第N回」で並べる。番号が読めないものは skipped に返す。"""
    lectures: dict[int, Lecture] = {}
    skipped: list[Path] = []
    for path in sorted(pdf_dir.iterdir()):
        if path.suffix.lower() != ".pdf" or path.name.startswith("."):
            continue
        n = lecture_number_of(path)
        if n is None:
            skipped.append(path)
            continue
        if n in lectures:
            print(f"[warn] 第{n}回 が重複: {lectures[n].path.name} と {path.name}。前者を使う", file=sys.stderr)
            continue
        lectures[n] = Lecture(number=n, path=path)
    return [lectures[k] for k in sorted(lectures)], skipped


def extract_pages(pdf_path: Path) -> list[str]:
    """pdftotext でページごとのテキストを返す（正規化済み）。ページ区切りは form feed。"""
    out = subprocess.run(
        ["pdftotext", "-enc", "UTF-8", str(pdf_path), "-"],
        check=True, capture_output=True,
    ).stdout.decode("utf-8", errors="replace")
    pages = out.split("\f")
    if pages and pages[-1].strip() == "":
        pages = pages[:-1]
    return [normalize_text(p) for p in pages]


def load_lectures(pdf_dir: Path) -> tuple[list[Lecture], list[Path]]:
    lectures, skipped = find_lectures(pdf_dir)
    leftover: set[str] = set()
    for lec in lectures:
        lec.pages = extract_pages(lec.path)
        for page in lec.pages:
            leftover.update(ch for ch in page if "\u2e80" <= ch <= "\u2fdf")
    if leftover:
        print(f"[warn] 正規化で戻らなかった部首文字: {''.join(sorted(leftover))} "
              f"(quizmap/common.py の RADICAL_SUPPLEMENT_MAP に追加すること)", file=sys.stderr)
    return lectures, skipped


def load_lecture_files(pairs: list[tuple[Path, int]]) -> list[Lecture]:
    """(PDF のパス, 講義回) の組から Lecture を作り、ページを読む。アプリの取り込み画面用。"""
    lectures = [Lecture(number=n, path=path) for path, n in sorted(pairs, key=lambda x: x[1])]
    for lec in lectures:
        lec.pages = extract_pages(lec.path)
    return lectures


def lecture_as_prompt_text(lec: Lecture) -> str:
    """LLM に渡す形。ページ番号を必ず付ける（出典に使わせる）。"""
    parts = [f"=== 第{lec.number}回 ==="]
    for i, page in enumerate(lec.pages, start=1):
        parts.append(f"[第{lec.number}回 p.{i}] {page}")
    return "\n".join(parts)


# ---------------------------------------------------------------- json io

def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


_UMASK = _umask()


def write_json(path: Path, data) -> None:
    """JSON を書く。同じフォルダの一時ファイルに書いてから os.replace で置き換える（#54）。

    読む側には、置き換える前の完全なファイルか、置き換えた後の完全なファイルのどちらかが見える。
    書いている途中の空や半端なファイルは見えない。別のスレッドが同じファイルを同時に書いても、
    一時ファイルはそれぞれ別の名前なので混ざらない（最後に置き換えたものが残る）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o666 & ~_UMASK)          # mkstemp は 0600 で作るので、普通のファイルと同じ権限に戻す
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# ---------------------------------------------------------------- llm helper

_rate_lock = threading.Lock()
_last_call_at: dict[str, float] = {}     # 枠ごと（generate / embed）の直前の呼び出しの時刻
_backoff = 1.0                           # 生成の呼び出し間隔の倍率（429 で延ばし、静かなら戻す）
_last_429_at = -1e9


class LLMError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class DailyQuotaError(LLMError):
    """1 日あたりの無料枠を使い切った 429。待っても今日は通らないので、再試行しない（#107）。"""


_DAILY_RE = re.compile(r"per\s*day|perday|per_day|daily", re.I)


def is_daily_quota(message: str) -> bool:
    """429 の本文（と quota の名前）が、1 日あたりの枠のものか。"""
    return bool(_DAILY_RE.search(message or ""))


def _bucket(url: str) -> str:
    """呼び出しの枠。埋め込みと生成は別の枠なので、互いに待たない（埋め込みの量は rag._pace が守る）。"""
    return "embed" if "EmbedContents" in url else "generate"


def current_interval() -> float:
    """いまの生成の呼び出し間隔（秒）。"""
    return min(max(MIN_CALL_INTERVAL, MIN_CALL_INTERVAL * _backoff), max(MIN_CALL_INTERVAL, MAX_CALL_INTERVAL))


def _note_429() -> None:
    global _backoff, _last_429_at
    with _rate_lock:
        cap = MAX_CALL_INTERVAL / MIN_CALL_INTERVAL if MIN_CALL_INTERVAL > 0 else 1.0
        _backoff = min(max(1.0, cap), _backoff * INTERVAL_BACKOFF)
        _last_429_at = time.monotonic()


def _note_success() -> None:
    global _backoff, _last_429_at
    with _rate_lock:
        if _backoff > 1.0 and time.monotonic() - _last_429_at >= INTERVAL_RECOVER_AFTER:
            _backoff = max(1.0, _backoff / INTERVAL_BACKOFF)
            _last_429_at = time.monotonic()        # 次の 1 段も、また静かな時間を待ってから戻す


def _wait_for_slot(bucket: str = "generate") -> None:
    """呼び出しの間に待機を入れる（スレッド間で共有。無料枠の RPM 対策）。生成の枠だけ。"""
    if bucket != "generate":
        return
    with _rate_lock:
        t = time.monotonic()
        wait = _last_call_at.get(bucket, -1e9) + current_interval() - t
        if wait > 0:
            time.sleep(wait)
        _last_call_at[bucket] = time.monotonic()


def _error_message(resp) -> str:
    """エラーの本文。quota の名前（details の quotaId・quotaMetric）も足す（日ごとの枠を見分けるため）。"""
    try:
        data = resp.json()
    except ValueError:
        return resp.text
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return resp.text
    message = err.get("message", resp.text)
    names = []
    for d in err.get("details") or []:
        for v in (d.get("violations") or []) if isinstance(d, dict) else []:
            names += [v.get(k) for k in ("quotaId", "quotaMetric") if v.get(k)]
    return message + (f" [quota: {', '.join(names)}]" if names else "")


def _retry_delay(message: str, attempt: int) -> float:
    """429 のとき API が retryDelay を返していればそれを優先、なければ指数バックオフ。"""
    base = min(60.0, 4.0 * (2 ** attempt))
    m = re.search(r"retry(?:Delay|.in)\D*(\d+(?:\.\d+)?)\s*s", message, re.I)
    if m:
        base = max(base, float(m.group(1)))
    return base + random.uniform(0, 1)


def _post_with_retry(client: LLMClient, url: str, body: dict, retry_on_429: bool = True) -> dict:
    """POST して JSON を返す。429・5xx・通信エラーは待って再試行する。

    retry_on_429=False なら、429 は待たずにすぐ LLMError(429) を投げる（呼ぶ側が量を減らして送り直すため。
    rag.embed がバッチを割るのに使う）。5xx と通信エラーはこれまでどおり再試行する。
    1 日あたりの枠の 429 は、どちらでも待たずに DailyQuotaError を投げる（#107）。
    生成の 429 では呼び出し間隔を延ばす（current_interval）。
    """
    import requests

    bucket = _bucket(url)
    for attempt in range(MAX_RETRIES + 1):
        _wait_for_slot(bucket)
        try:
            resp = requests.post(url, headers=client._headers(), json=body, timeout=client.timeout)
        except requests.RequestException as e:
            if attempt == MAX_RETRIES:
                raise
            delay = _retry_delay("", attempt)
            print(f"  [retry] 通信エラー {type(e).__name__}: {delay:.0f} 秒待って再試行 ({attempt + 1}/{MAX_RETRIES})",
                  file=sys.stderr)
            time.sleep(delay)
            continue
        if resp.status_code == 200:
            if bucket == "generate":
                _note_success()
            return resp.json()
        message = _error_message(resp)
        if resp.status_code == 429:
            if is_daily_quota(message):
                raise DailyQuotaError(429, message)
            if bucket == "generate":
                _note_429()
            if not retry_on_429:
                raise LLMError(429, message)
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_RETRIES:
                raise LLMError(resp.status_code, message)
            delay = _retry_delay(message, attempt)
            print(f"  [retry] HTTP {resp.status_code}: {delay:.0f} 秒待って再試行 ({attempt + 1}/{MAX_RETRIES})"
                  + (f"、呼び出し間隔 {current_interval():.1f} 秒" if bucket == "generate" else ""),
                  file=sys.stderr)
            time.sleep(delay)
            continue
        raise LLMError(resp.status_code, message)
    raise RuntimeError("unreachable")


def call_json(client: LLMClient, *, model: str, system: str, user: str, schema: dict,
              max_tokens: int = 32000) -> dict:
    """構造化出力（JSON Schema 指定）で 1 回呼び、dict を返す。プロバイダ非依存の入口。"""
    url = f"{client.base_url}/models/{model}:generateContent"
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": schema,
            "maxOutputTokens": max_tokens,
            "temperature": 0.2,
        },
    }
    for attempt in range(MAX_RETRIES + 1):
        data = _post_with_retry(client, url, body)
        cands = data.get("candidates") or []
        if not cands:
            raise RuntimeError(f"本文が返らなかった: {json.dumps(data, ensure_ascii=False)[:500]}")
        cand = cands[0]
        finish = cand.get("finishReason", "")
        if finish == "MAX_TOKENS":
            raise RuntimeError("出力が maxOutputTokens で切れた。max_tokens を増やすか入力を分割すること")
        parts = cand.get("content", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text:
            raise RuntimeError(f"本文が空 (finishReason={finish}): {json.dumps(cand, ensure_ascii=False)[:500]}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            if attempt == MAX_RETRIES:
                raise
            print(f"  [retry] JSON が壊れていた ({e}). 再試行 ({attempt + 1}/{MAX_RETRIES})", file=sys.stderr)
    raise RuntimeError("unreachable")


def list_models(client: LLMClient) -> list[dict]:
    """利用できるモデル（generateContent 対応）。name / displayName / inputTokenLimit / outputTokenLimit。"""
    import requests

    models, token = [], None
    while True:
        params = {"pageSize": 100}
        if token:
            params["pageToken"] = token
        resp = requests.get(f"{client.base_url}/models", headers=client._headers(), params=params,
                            timeout=client.timeout)
        if resp.status_code != 200:
            raise LLMError(resp.status_code, resp.text[:300])
        data = resp.json()
        for m in data.get("models", []):
            if "generateContent" in m.get("supportedGenerationMethods", []):
                models.append({
                    "name": m["name"].removeprefix("models/"),
                    "displayName": m.get("displayName", ""),
                    "inputTokenLimit": m.get("inputTokenLimit"),
                    "outputTokenLimit": m.get("outputTokenLimit"),
                })
        token = data.get("nextPageToken")
        if not token:
            return models
