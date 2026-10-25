"""RAG の部品（docs/rag-design.md）。スライドの索引と Wikipedia の引き当て。

- embed(): Gemini の batchEmbedContents で文を埋め込む。一度埋め込んだ文は DATA_DIR/embed_cache/ に残し、
  コースをまたいで使い回す（同じ資料の取り込み直しでは API を呼ばない）。バッチは件数と文字数の両方で小さくし、
  1 分あたりの件数・トークン数の上限を超えないよう間を空ける。429 ならバッチを半分に割って送り直す
- SlideIndex: pages.json の全ページを埋め込み、コサイン類似度で引く。course_dir/rag_index.json に保存する
  （ベクトルは長さ 1 に正規化してから float16 にし、base64 で 1 本の文字列にする。185 ページで約 1.5 MB）
- Wiki: MediaWiki API で概念名を検索し、最初の 1 件の記事の冒頭を返す。course_dir/wiki_cache.json に残し、
  同じ語で二度問い合わせない

Wikipedia は関係の確認と選択肢の検証にだけ使う。答えの根拠はスライドだけ（設計書の方針 2〜4）。
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import sys
import unicodedata
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import quote

import numpy as np

from quizmap.common import DATA_DIR, LLMClient, _post_with_retry, read_json, write_json

EMBED_MODEL = os.environ.get("QUIZMAP_EMBED_MODEL", "gemini-embedding-001")
EMBED_MAX_CHARS = 6000         # gemini-embedding-001 の入力上限（2048 トークン）を超えないよう切る
# 無料枠は、バッチの 1 件ずつが 1 リクエストと数えられ、1 分あたりのトークン数にも上限がある。
# 100 件（英語で約 7 万字）を 1 回で送ると、それだけで 1 分の枠を超えて 429 が続いた。
EMBED_BATCH = int(os.environ.get("QUIZMAP_EMBED_BATCH", "20"))                  # 1 回に送る件数の上限
EMBED_BATCH_CHARS = int(os.environ.get("QUIZMAP_EMBED_BATCH_CHARS", "20000"))   # 1 回に送る文字数の合計の上限
EMBED_BATCH_TOKENS = int(os.environ.get("QUIZMAP_EMBED_BATCH_TOKENS", "20000"))  # 1 回に送るトークン数（見積もり）の上限
# 無料枠: embed_content_free_tier_tokens は 1 分あたり 30,000 トークン。見積もりの誤差を見て少し下で止める
EMBED_ITEMS_PER_MIN = int(os.environ.get("QUIZMAP_EMBED_ITEMS_PER_MIN", "80"))   # 1 分あたりの件数（無料枠 100 より下）
EMBED_TOKENS_PER_MIN = int(os.environ.get("QUIZMAP_EMBED_TOKENS_PER_MIN", "25000"))  # 1 分あたりのトークン数（見積もり）
# 埋め込みのキャッシュ（コースをまたいで共有）。キーは モデル・task・本文（EMBED_MAX_CHARS で切った後）の sha256
EMBED_CACHE_DIR = Path(os.environ.get("QUIZMAP_EMBED_CACHE_DIR", DATA_DIR / "embed_cache"))
EMBED_429_RETRIES = 4          # 1 件まで割っても 429 のとき、待って送り直す回数
EMBED_429_MIN_WAIT = 10.0      # 429 の後、割ったバッチを送る前に待つ最小の秒数
INDEX_FILE = "rag_index.json"
INDEX_VERSION = 1
WIKI_CACHE_FILE = "wiki_cache.json"
WIKI_USER_AGENT = ("LectureQuizMaker/0.1 (ICU LNG302 student project; "
                   "https://github.com/chiiiiinamiiiii/lecture-quiz-maker) python-requests")
WIKI_TIMEOUT = 20.0
WIKI_MAX_EXTRACT = 3000        # 冒頭が長い記事はここで切る（キャッシュを小さく保つ）


# ---------------------------------------------------------------- embed

_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff\uff66-\uff9f]")


def approx_tokens(text: str) -> int:
    """トークン数の概算。日本語・中国語の文字は 1 字 1 トークン、それ以外は 4 字 1 トークン。"""
    cjk = len(_CJK_RE.findall(text))
    return cjk + (len(text) - cjk + 3) // 4


def split_batches(items: list[tuple[int, str]], max_items: int = None, max_chars: int = None,
                  max_tokens: int = None) -> list[list]:
    """[(添字, 文)] を、件数・文字数の合計・トークン数（見積もり）の合計のどれも上限を超えないバッチに分ける。

    順は保つ。1 件だけで上限を超えるものは、その 1 件だけのバッチにする。
    """
    max_items = max_items or EMBED_BATCH
    max_chars = max_chars or EMBED_BATCH_CHARS
    max_tokens = max_tokens or min(EMBED_BATCH_TOKENS, EMBED_TOKENS_PER_MIN)
    batches, cur, chars, tokens = [], [], 0, 0
    for it in items:
        n, k = len(it[1]), approx_tokens(it[1])
        if cur and (len(cur) >= max_items or chars + n > max_chars or tokens + k > max_tokens):
            batches.append(cur)
            cur, chars, tokens = [], 0, 0
        cur.append(it)
        chars += n
        tokens += k
    if cur:
        batches.append(cur)
    return batches


# 直近 1 分に送った量（時刻, 件数, トークン数）。プロセスの中で共有する
_sent: deque = deque()
_sent_lock = threading.Lock()


def _now() -> float:
    return time.monotonic()


def _sleep(sec: float) -> None:
    time.sleep(sec)


_RETRY_IN_RE = re.compile(r"retry in\s*(\d+(?:\.\d+)?)\s*s", re.I)


def _wait_after_429(message: str, attempt: int) -> float:
    """429 の後に待つ秒数。本文に「Please retry in N s」があればその秒数（＋1 秒）、無ければ指数バックオフ。"""
    from quizmap.common import _retry_delay

    m = _RETRY_IN_RE.search(message or "")
    if m:
        return float(m.group(1)) + 1.0
    return max(EMBED_429_MIN_WAIT, _retry_delay(message or "", attempt))


def _pace(n_items: int, n_tokens: int) -> None:
    """これから送る量を足しても、直近 60 秒の件数・トークン数が上限を超えないところまで待つ。"""
    with _sent_lock:
        while True:
            t = _now()
            while _sent and t - _sent[0][0] >= 60.0:
                _sent.popleft()
            items = sum(x[1] for x in _sent)
            tokens = sum(x[2] for x in _sent)
            if not _sent or (items + n_items <= EMBED_ITEMS_PER_MIN and tokens + n_tokens <= EMBED_TOKENS_PER_MIN):
                _sent.append((t, n_items, n_tokens))
                return
            _sleep(max(0.5, 60.0 - (t - _sent[0][0])))      # いちばん古い記録が 1 分を過ぎるまで


def _cache_path(task: str, text: str) -> Path:
    h = hashlib.sha256(f"{EMBED_MODEL}\n{task}\n{text}".encode("utf-8")).hexdigest()
    return EMBED_CACHE_DIR / EMBED_MODEL / task / h[:2] / f"{h}.f32"


def cache_get(task: str, text: str) -> list[float] | None:
    """キャッシュにあるベクトル（無い・壊れていれば None）。"""
    try:
        raw = _cache_path(task, text).read_bytes()
    except OSError:
        return None
    if not raw or len(raw) % 4:
        return None
    return np.frombuffer(raw, dtype="<f4").astype(float).tolist()


def cache_put(task: str, text: str, vector: list[float]) -> None:
    """キャッシュに書く。置き換えで書くので、途中で止まっても壊れたファイルは残らない。書けなくても止めない。"""
    path = _cache_path(task, text)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_bytes(np.asarray(vector, dtype="<f4").tobytes())
        os.replace(tmp, path)
    except OSError as e:
        print(f"[warn] 埋め込みのキャッシュに書けなかった: {e}", file=sys.stderr)


def embed(client: LLMClient, texts: list[str], task: str = "RETRIEVAL_DOCUMENT",
          progress=None) -> list[list[float]]:
    """texts を埋め込み、入力と同じ順でベクトルを返す。task は RETRIEVAL_DOCUMENT / RETRIEVAL_QUERY など。

    空の文字列は API が受け付けないので、埋め込まずに長さ 0 のベクトル（全要素 0）を返す。
    バッチは件数 EMBED_BATCH・文字数 EMBED_BATCH_CHARS・トークン数 EMBED_BATCH_TOKENS（見積もり）まで。
    送る前に _pace で、直前 60 秒に送った件数・トークン数が 1 分あたりの上限を超えないところまで待つ。
    429 のときはそのバッチを半分に割って送り直す。1 件でも 429 なら、待って EMBED_429_RETRIES 回まで送り直す。
    progress(済んだ件数, 全件数) をバッチが済むたびに呼ぶ（索引づくりの進み具合を画面に出すため）。
    キャッシュにある文は送らない。バッチが通るたびにキャッシュに書くので、途中で失敗しても、
    次に呼んだときは残りの文だけを送る。
    """
    from quizmap.common import DailyQuotaError, LLMError

    out: list[list[float] | None] = [None] * len(texts)
    todo = [(i, t[:EMBED_MAX_CHARS]) for i, t in enumerate(texts) if t and t.strip()]
    url = f"{client.base_url}/models/{EMBED_MODEL}:batchEmbedContents"
    dim = 0
    total = len(todo)
    missing = []
    for i, t in todo:
        v = cache_get(task, t)
        if v is None:
            missing.append((i, t))
        else:
            out[i] = v
            dim = len(v)
    done = total - len(missing)
    if done and progress:
        progress(done, total)

    def send(chunk: list[tuple[int, str]]) -> None:
        nonlocal dim, done
        body = {"requests": [
            {"model": f"models/{EMBED_MODEL}", "content": {"parts": [{"text": t}]}, "taskType": task}
            for _, t in chunk
        ]}
        for attempt in range(EMBED_429_RETRIES + 1):
            _pace(len(chunk), sum(approx_tokens(t) for _, t in chunk))
            try:
                data = _post_with_retry(client, url, body, retry_on_429=False)
                break
            except LLMError as e:
                if e.status != 429 or isinstance(e, DailyQuotaError):
                    raise                                 # 日ごとの枠は待っても通らない（#107）
                wait = _wait_after_429(str(e), attempt)
                if len(chunk) > 1:
                    half = len(chunk) // 2
                    print(f"  [embed] 429: {len(chunk)} 件を {half} + {len(chunk) - half} 件に割り、"
                          f"{wait:.0f} 秒待って送り直す", file=sys.stderr)
                    _sleep(wait)
                    send(chunk[:half])
                    send(chunk[half:])
                    return
                if attempt == EMBED_429_RETRIES:
                    raise
                print(f"  [embed] 429（1 件）: {wait:.0f} 秒待って送り直す ({attempt + 1}/{EMBED_429_RETRIES})",
                      file=sys.stderr)
                _sleep(wait)
        embs = data.get("embeddings") or []
        if len(embs) != len(chunk):
            raise RuntimeError(f"埋め込みの数が合わない: {len(chunk)} 件送って {len(embs)} 件返った")
        for (i, t), e in zip(chunk, embs):
            out[i] = e["values"]
            dim = len(e["values"])
            cache_put(task, t, e["values"])
        done += len(chunk)
        if progress:
            progress(done, total)

    for chunk in split_batches(missing):
        send(chunk)
    return [v if v is not None else [0.0] * dim for v in out]


def _normalize(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


# ---------------------------------------------------------------- slide index

class SlideIndex:
    """スライドのページの索引。pages は pages.json と同じ形、vectors は長さ 1 に正規化した float16。"""

    def __init__(self, pages: list[dict], vectors: np.ndarray, model: str = EMBED_MODEL):
        self.pages = [{"lecture": int(p["lecture"]), "page": int(p["page"]), "text": p.get("text", "")}
                      for p in pages]
        self.vectors = vectors.astype(np.float16)
        self.model = model
        self._query_cache: dict[str, np.ndarray] = {}   # 同じ問い合わせ文を二度埋め込まない

    def __len__(self) -> int:
        return len(self.pages)

    @classmethod
    def build(cls, client: LLMClient, pages: list[dict], progress=None) -> "SlideIndex":
        """progress(済んだページ数, 本文のあるページ数) をバッチごとに呼ぶ。"""
        vecs = embed(client, [p.get("text", "") for p in pages], task="RETRIEVAL_DOCUMENT", progress=progress)
        dim = len(vecs[0]) if vecs else 0
        m = np.asarray(vecs, dtype=np.float32).reshape(len(pages), dim)
        return cls(pages, _normalize(m))

    def save(self, course_dir: Path) -> None:
        dim = self.vectors.shape[1]
        raw = np.ascontiguousarray(self.vectors, dtype="<f2").tobytes()
        write_json(Path(course_dir) / INDEX_FILE, {
            "version": INDEX_VERSION,
            "model": self.model,
            "dim": int(dim),
            "dtype": "float16-le",
            "pages": self.pages,
            "vectors": base64.b64encode(raw).decode("ascii"),
        })

    @classmethod
    def load(cls, course_dir: Path) -> "SlideIndex | None":
        """索引が無い・壊れている・別のモデルで作った場合は None（呼ぶ側は索引なしで続ける）。"""
        path = Path(course_dir) / INDEX_FILE
        if not path.exists():
            return None
        try:
            data = read_json(path)
            if data.get("model") != EMBED_MODEL:
                print(f"[warn] 索引のモデル {data.get('model')} が {EMBED_MODEL} と違うので使わない",
                      file=sys.stderr)
                return None
            dim = int(data["dim"])
            raw = base64.b64decode(data["vectors"])
            m = np.frombuffer(raw, dtype="<f2").reshape(len(data["pages"]), dim)
            return cls(data["pages"], m, model=data["model"])
        except (ValueError, KeyError, TypeError) as e:
            print(f"[warn] 索引 {path} を読めなかった: {e}", file=sys.stderr)
            return None

    def _query_vector(self, client: LLMClient, query: str) -> np.ndarray:
        v = self._query_cache.get(query)
        if v is None:
            v = np.asarray(embed(client, [query], task="RETRIEVAL_QUERY")[0], dtype=np.float32)
            n = np.linalg.norm(v)
            v = v / n if n else v
            self._query_cache[query] = v
        return v

    def search(self, client: LLMClient, query: str, k: int = 5,
               exclude: set[tuple[int, int]] = frozenset()) -> list[dict]:
        """query に近いページを k 件。[{"lecture", "page", "text", "score"}]（score はコサイン類似度、降順）。

        exclude の (講義回, ページ) と本文が空のページは返さない。
        """
        if not self.pages or k <= 0 or not query.strip():
            return []
        return self._rank(self._query_vector(client, query), k, exclude)

    def search_many(self, client: LLMClient, queries: list[str], k: int = 5,
                    exclude: set[tuple[int, int]] = frozenset()) -> list[list[dict]]:
        """search を複数の問い合わせ文でまとめて行う。結果は queries と同じ順の search の結果のリスト。

        まだ埋め込んでいない文だけを 1 回の batchEmbedContents（100 件ずつ）でまとめて埋め込み、
        search と同じキャッシュに入れる。重複した文は 1 回だけ送る。
        """
        if not self.pages or k <= 0:
            return [[] for _ in queries]
        todo = list(dict.fromkeys(q for q in queries if q.strip() and q not in self._query_cache))
        if todo:
            for q, v in zip(todo, embed(client, todo, task="RETRIEVAL_QUERY")):
                v = np.asarray(v, dtype=np.float32)
                n = np.linalg.norm(v)
                self._query_cache[q] = v / n if n else v
        return [self._rank(self._query_cache[q], k, exclude) if q.strip() else [] for q in queries]

    def _rank(self, q: np.ndarray, k: int, exclude: set[tuple[int, int]]) -> list[dict]:
        scores = self.vectors.astype(np.float32) @ q
        hits = []
        for i in np.argsort(-scores, kind="stable"):
            p = self.pages[i]
            if (p["lecture"], p["page"]) in exclude or not p["text"].strip():
                continue
            hits.append({**p, "score": round(float(scores[i]), 4)})
            if len(hits) >= k:
                break
        return hits


# ---------------------------------------------------------------- wikipedia

_PAREN_RE = re.compile(r"\([^()]*\)")                  # NFKC の後なので全角の括弧も半角になっている
_EDGE_RE = re.compile(r"^[\W_]+|[\W_]+$")              # 前後の記号・空白（漢字・かな・英数字は \w）
_SPACE_RE = re.compile(r"\s+")
_KEY_DROP_RE = re.compile(r"[\W_]+")                    # 題名との比べ方: 空白・中黒・記号は無視する


def normalize_term(term: str) -> str:
    """Wikipedia で引く語にする（#93）。

    NFKC にそろえ、括弧の中（読み・原語・曖昧さ回避。「功利主義（こうりしゅぎ）」「Kant (philosopher)」）を外し、
    前後の記号と空白を削り、中の空白を 1 つにする。
    """
    t = unicodedata.normalize("NFKC", term or "")
    prev = None
    while prev != t:                                     # 入れ子の括弧も外す
        prev, t = t, _PAREN_RE.sub(" ", t)
    t = _EDGE_RE.sub("", t)
    return _SPACE_RE.sub(" ", t).strip()


def _match_key(text: str) -> str:
    return _KEY_DROP_RE.sub("", normalize_term(text)).casefold()


def title_matches(term: str, title: str) -> bool:
    """記事の題名が引いた語と合うか（#93）。正規化して比べる。

    - 語が題名に含まれれば合う（カント → イマヌエル・カント）
    - 題名が語に含まれるときは、題名が語の半分より長いときだけ合う。
      語より広い短い記事（自己目的 → 目的、理性的存在者 → 理性）は使わない
    """
    a, b = _match_key(term), _match_key(title)
    if not (a and b):
        return False
    return a in b or (b in a and len(b) * 2 > len(a))


class Wiki:
    """Wikipedia の記事の冒頭を引く。lang は "ja" / "en"。結果（見つからなかったことも）をキャッシュする。

    引く前に語を正規化し（normalize_term）、キャッシュのキーも正規化後の語にする。
    検索の 1 件目の題名が語と合わない（title_matches でない）ときは、関係の無い記事なので None にする（#93）。
    通信に失敗したときは None を返し、キャッシュしない（次に呼んだときにもう一度試す）。
    """

    def __init__(self, course_dir: Path, lang: str):
        self.lang = lang if lang in ("ja", "en") else "en"
        self.path = Path(course_dir) / WIKI_CACHE_FILE
        self.api = f"https://{self.lang}.wikipedia.org/w/api.php"
        self._lock = threading.Lock()
        self._cache: dict[str, dict | None] = {}
        if self.path.exists():
            try:
                self._cache = dict(read_json(self.path).get(self.lang, {}))
            except (ValueError, AttributeError) as e:
                print(f"[warn] {self.path} を読めなかった: {e}", file=sys.stderr)

    def lookup(self, term: str) -> dict | None:
        """{"title", "url", "extract"}。見つからない・題名が語と合わなければ None。"""
        term = normalize_term(term)
        if not term:
            return None
        with self._lock:
            if term in self._cache:
                hit = self._cache[term]
                # #93 より前のキャッシュには、題名の合わない記事が残っていることがある
                return hit if hit is None or title_matches(term, hit.get("title", "")) else None
        try:
            result = self._fetch(term)
        except Exception as e:  # noqa: BLE001  通信の失敗で取り込みや出題を止めない
            print(f"[warn] Wikipedia の検索に失敗 ({term}): {type(e).__name__}: {e}", file=sys.stderr)
            return None
        with self._lock:
            self._cache[term] = result
            self._save()
        return result

    def _get(self, params: dict) -> dict:
        import requests

        resp = requests.get(self.api, params={**params, "format": "json", "formatversion": 2},
                            headers={"User-Agent": WIKI_USER_AGENT}, timeout=WIKI_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def _fetch(self, term: str) -> dict | None:
        found = self._get({"action": "query", "list": "search", "srsearch": term,
                           "srlimit": 1, "srnamespace": 0}).get("query", {}).get("search") or []
        if not found:
            return None
        title = found[0]["title"]
        if not title_matches(term, title):
            return None                                  # 関係の無い記事。「見つからない」として残す
        pages = self._get({"action": "query", "prop": "extracts|info", "exintro": 1, "explaintext": 1,
                           "inprop": "url", "redirects": 1, "titles": title}).get("query", {}).get("pages") or []
        page = next((p for p in pages if not p.get("missing")), None)
        if page is None:
            return None
        extract = (page.get("extract") or "").strip()
        if not extract:
            return None
        title = page.get("title", title)
        url = page.get("fullurl") or f"https://{self.lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"
        return {"title": title, "url": url, "extract": extract[:WIKI_MAX_EXTRACT]}

    def _save(self) -> None:
        data = {}
        if self.path.exists():
            try:
                data = read_json(self.path)
            except ValueError:
                data = {}
        data[self.lang] = self._cache
        write_json(self.path, data)
