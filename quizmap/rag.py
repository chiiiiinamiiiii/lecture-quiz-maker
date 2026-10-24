"""RAG の部品（docs/rag-design.md）。スライドの索引と Wikipedia の引き当て。

- embed(): Gemini の batchEmbedContents で文を埋め込む。100 件ずつ、待機と再試行は common._post_with_retry に任せる
- SlideIndex: pages.json の全ページを埋め込み、コサイン類似度で引く。course_dir/rag_index.json に保存する
  （ベクトルは長さ 1 に正規化してから float16 にし、base64 で 1 本の文字列にする。185 ページで約 1.5 MB）
- Wiki: MediaWiki API で概念名を検索し、最初の 1 件の記事の冒頭を返す。course_dir/wiki_cache.json に残し、
  同じ語で二度問い合わせない

Wikipedia は関係の確認と選択肢の検証にだけ使う。答えの根拠はスライドだけ（設計書の方針 2〜4）。
"""
from __future__ import annotations

import base64
import os
import re
import sys
import unicodedata
import threading
from pathlib import Path
from urllib.parse import quote

import numpy as np

from quizmap.common import LLMClient, _post_with_retry, read_json, write_json

EMBED_MODEL = os.environ.get("QUIZMAP_EMBED_MODEL", "gemini-embedding-001")
EMBED_BATCH = 100              # batchEmbedContents の 1 回あたりの上限
EMBED_MAX_CHARS = 6000         # gemini-embedding-001 の入力上限（2048 トークン）を超えないよう切る
INDEX_FILE = "rag_index.json"
INDEX_VERSION = 1
WIKI_CACHE_FILE = "wiki_cache.json"
WIKI_USER_AGENT = ("LectureQuizMaker/0.1 (ICU LNG302 student project; "
                   "https://github.com/chiiiiinamiiiii/lecture-quiz-maker) python-requests")
WIKI_TIMEOUT = 20.0
WIKI_MAX_EXTRACT = 3000        # 冒頭が長い記事はここで切る（キャッシュを小さく保つ）


# ---------------------------------------------------------------- embed

def embed(client: LLMClient, texts: list[str], task: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    """texts を埋め込み、入力と同じ順でベクトルを返す。task は RETRIEVAL_DOCUMENT / RETRIEVAL_QUERY など。

    空の文字列は API が受け付けないので、埋め込まずに長さ 0 のベクトル（全要素 0）を返す。
    """
    out: list[list[float] | None] = [None] * len(texts)
    todo = [(i, t[:EMBED_MAX_CHARS]) for i, t in enumerate(texts) if t and t.strip()]
    url = f"{client.base_url}/models/{EMBED_MODEL}:batchEmbedContents"
    dim = 0
    for start in range(0, len(todo), EMBED_BATCH):
        chunk = todo[start:start + EMBED_BATCH]
        body = {"requests": [
            {"model": f"models/{EMBED_MODEL}", "content": {"parts": [{"text": t}]}, "taskType": task}
            for _, t in chunk
        ]}
        data = _post_with_retry(client, url, body)
        embs = data.get("embeddings") or []
        if len(embs) != len(chunk):
            raise RuntimeError(f"埋め込みの数が合わない: {len(chunk)} 件送って {len(embs)} 件返った")
        for (i, _), e in zip(chunk, embs):
            out[i] = e["values"]
            dim = len(e["values"])
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
    def build(cls, client: LLMClient, pages: list[dict]) -> "SlideIndex":
        vecs = embed(client, [p.get("text", "") for p in pages], task="RETRIEVAL_DOCUMENT")
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
    """記事の題名が引いた語と合うか。正規化して、どちらかがもう一方に含まれれば合う（#93）。"""
    a, b = _match_key(term), _match_key(title)
    return bool(a and b) and (a in b or b in a)


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
