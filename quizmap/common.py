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
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
# 仕様 §10: 無料枠で使える最も安いモデル。環境変数 QUIZMAP_MODEL か --model で切り替えられる。
# 2026-10-19 に /models と料金表で確認: 最安の gemini-2.5-flash-lite は新規ユーザーに提供終了（404）、
# 次に安い gemini-3.1-flash-lite（$0.25 / $1.50 per 1M tokens、無料枠あり）を既定にした
DEFAULT_MODEL = os.environ.get("QUIZMAP_MODEL", "gemini-3.1-flash-lite")
# 無料枠のレート制限対策。呼び出し間隔の下限（秒）。環境変数で調整できる
MIN_CALL_INTERVAL = float(os.environ.get("QUIZMAP_MIN_INTERVAL", "6"))
MAX_RETRIES = 6
OTHER_LABEL = "その他"  # 仕様 §3: 当てはまらない関係のためのラベル。常に 1 つ用意する

LECTURE_RE = re.compile(r"第\s*(\d+)\s*回")


# ---------------------------------------------------------------- env / client

def load_dotenv(path: Path = PROJECT_DIR / ".env") -> None:
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


def make_client() -> LLMClient:
    load_dotenv()
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit(
            "[error] GEMINI_API_KEY がない。Google AI Studio (https://aistudio.google.com/apikey) で\n"
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
    m = LECTURE_RE.search(unicodedata.normalize("NFKC", path.stem))
    return int(m.group(1)) if m else None


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


def lecture_as_prompt_text(lec: Lecture) -> str:
    """LLM に渡す形。ページ番号を必ず付ける（出典に使わせる）。"""
    parts = [f"=== 第{lec.number}回 ==="]
    for i, page in enumerate(lec.pages, start=1):
        parts.append(f"[第{lec.number}回 p.{i}] {page}")
    return "\n".join(parts)


# ---------------------------------------------------------------- json io

def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- llm helper

_rate_lock = threading.Lock()
_last_call_at = 0.0


class LLMError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def _wait_for_slot() -> None:
    """呼び出しの間に待機を入れる（スレッド間で共有。無料枠の RPM 対策）。"""
    global _last_call_at
    with _rate_lock:
        now = time.monotonic()
        wait = _last_call_at + MIN_CALL_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()


def _retry_delay(message: str, attempt: int) -> float:
    """429 のとき API が retryDelay を返していればそれを優先、なければ指数バックオフ。"""
    base = min(60.0, 4.0 * (2 ** attempt))
    m = re.search(r"retry(?:Delay|.in)\D*(\d+(?:\.\d+)?)\s*s", message, re.I)
    if m:
        base = max(base, float(m.group(1)))
    return base + random.uniform(0, 1)


def _post_with_retry(client: LLMClient, url: str, body: dict) -> dict:
    import requests

    for attempt in range(MAX_RETRIES + 1):
        _wait_for_slot()
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
            return resp.json()
        try:
            message = resp.json().get("error", {}).get("message", resp.text)
        except ValueError:
            message = resp.text
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_RETRIES:
                raise LLMError(resp.status_code, message)
            delay = _retry_delay(message, attempt)
            print(f"  [retry] HTTP {resp.status_code}: {delay:.0f} 秒待って再試行 ({attempt + 1}/{MAX_RETRIES})",
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
