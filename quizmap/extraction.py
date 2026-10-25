"""§5 抽出。語彙（vocab）を固定して、概念 → 関係 の 2 段階で抽出し、graph.json・pages.json・slides/ を書く。

extract.py（コマンド）とアプリの取り込み画面の両方から呼ぶ。

守っていること:
- NFKC 正規化と改行除去（quizmap/common.py）
- 概念数に上限を設けない。1 度しか出ない概念も切り捨てない
- 概念を先に全部出させ、その id だけを使って関係を書かせる
- 概念の sources にページ番号を必ず持たせる
"""
from __future__ import annotations

import collections
import datetime as dt
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from quizmap.common import OTHER_LABEL, Lecture, call_json, normalize_text, write_json
from quizmap.vocab import kind_examples, language_name, language_of, language_rule

# ------------------------------------------------------------------ 概念と関係の抽出（かたまりごとに 1 回、#107）

EXTRACT_SYSTEM = """あなたは大学の授業資料から、学習者が理解すべき「概念」と、概念どうしの「関係」を漏れなく取り出す専門家です。
{language_rule}与えられた種類（kind）と関係ラベルだけを使います。"""

EXTRACT_USER = """授業「{course}」の第{lecture}回の資料のうち、p.{first}〜p.{last} のページです（ページ番号付き）。
このページに出てくる概念をすべて concepts に挙げ、それらの概念どうしの関係をすべて relations に挙げてください。

概念の種類（これ以外の kind は使わない。どれにも当てはまらなければ「{other}」）:
{kinds}

関係ラベル（これ以外は使わない。どれにも当てはまらなければ「{other}」）:
{relations}

概念について守ること:
- このページに出てくる概念はすべて挙げる。1 度しか出てこない概念も挙げる。数の上限はない
- 見出しだけでなく、本文・箇条書き・図表の中で説明されている用語・人物・立場・原則も挙げる
- スライドに出典・参考文献・引用元として書かれているだけの書名・報告書名・論文名は、概念として挙げない。
  その本の内容として説明されている概念や、著者の人物を挙げる
- 本の題名が、その本の主張する考え方の名前にもなっている場合（例: ある本の題名と同じ名前の学説）は、本とは別に、
  その考え方を概念として挙げる。その種類は【出典の文献】の種類にしない
- 授業の中で筋や内容が論じられている作品（劇・小説・報告書など）は、出典として挙がっているだけの本と区別する。
  作品そのものを【出典の文献】ではない種類で挙げ、内容を表す概念（その作品の中の対立や原則など）も挙げる
- 一方で、資料に書かれていない概念を足さない
- label は、そのページの本文に書かれている表記を、そのまま写す（言い換え・要約・表記の統一・送り仮名の変更をしない）。
  短い{language}の語句にする（文にしない）
- id は label をローマ字にした snake_case の ASCII（例: kant, gimu_ron）
- definition は資料の記述に基づいて 1〜2 文
- pages はその概念の名前が書かれているページの番号（p.{first}〜p.{last} から。複数可）。必ず 1 つ以上入れる

関係について守ること:
- from と to には、concepts に挙げた概念の label をそのまま書く。同じ概念を両端にしない
- 資料に明示的に書かれている関係だけを挙げる。一般知識から補わない
- label はラベルの定義に従って選ぶ。無理に当てはめず、合わなければ「{other}」にする
- from と to はラベルの定義が決める向きに従う。逆向きの関係ならば from と to を入れ替える
- from と to の概念の種類が、ラベルの【向き】に書かれた種類でなければ、そのラベルは使わない
- A が B の定義そのもの、または A と B が同じものの言い換えである場合、それに合うラベルが無ければ「{other}」にする。ほかのラベルに押し込まない
- page はその関係が書かれているページの番号（p.{first}〜p.{last} から）
- evidence には、その関係が書かれている箇所を page のページ本文からそのまま 40 字以内で抜き出す。抜き出せない関係は出さない
- 同じページに二つの概念が出てくるだけでは関係にしない。二つの概念を結びつける記述があるときだけ挙げる
- 1 度しか書かれていない関係も挙げる。数の上限はない

=== 資料 ===
{text}
"""

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "concepts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "label": {"type": "string"},
                    "kind": {"type": "string"},
                    "definition": {"type": "string"},
                    "pages": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["id", "label", "kind", "definition", "pages"],
                "additionalProperties": False,
            },
        },
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "label": {"type": "string"},
                    "page": {"type": "integer"},
                    "evidence": {"type": "string"},
                },
                "required": ["from", "to", "label", "page", "evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["concepts", "relations"],
    "additionalProperties": False,
}


_ID_RE = re.compile(r"[^a-z0-9_]+")


_SPACE_RE = re.compile(r"\s+")


def _squash(text: str) -> str:
    """出典の照合用。ページ本文と同じ正規化をかけ、空白をすべて除く。"""
    return _SPACE_RE.sub("", normalize_text(text))


def _label_head(label: str) -> str:
    """概念名の照合用。括弧より前（例: 「理（ロゴス）を伴う活動」→「理」）。"""
    return _squash(label).split("(")[0]


def check_evidence(evidence: str, end_labels: list[str], page_text: str) -> str | None:
    """関係が出典ページに根拠を持つか（Issue #11）。落とす理由、または None を返す。"""
    page = _squash(page_text)
    evidence = _squash(evidence)
    if not evidence or evidence not in page:
        return "evidence が本文に無い"
    heads = [_label_head(l) for l in end_labels]
    if not any(h and h in page for h in heads):
        return "両端の概念名が本文に無い"
    return None


def evidence_rank(evidence: str, end_labels: list[str], page_text: str) -> tuple[int, int]:
    """同じ対で同点のときに使う、根拠の確かさ（Issue #11・#12）。大きいほど確か。

    (evidence に出てくる端の概念名の数, ページ本文に出てくる端の概念名の数)。どちらも 0〜2。
    """
    page, evidence = _squash(page_text), _squash(evidence)
    heads = [h for h in (_label_head(l) for l in end_labels) if h]
    return sum(h in evidence for h in heads), sum(h in page for h in heads)



def clean_id(raw: str, fallback: str) -> str:
    s = _ID_RE.sub("_", raw.strip().lower()).strip("_")
    return s or fallback


CONCEPT_PAGES = int(os.environ.get("QUIZMAP_CONCEPT_PAGES", "4"))   # 概念の抽出の 1 回に渡すページ数の目安（0 以下で回ごと）
SURFACE_EXAMPLES = 20

# 地図に出す名前は、資料の本文に書かれている表記そのもの（利用者の決まり、#101）。
# LLM の label が本文に無ければ、まずコードで本文の中の一番近い表記を探し、それでも無ければ回ごとに 1 回だけ
# LLM に本文から写させて、本文にあることをコードで確かめる。それでも見つからない概念は地図の点にしないが、
# graph の concepts_without_surface に残す（消さない）。

SURFACE_SYSTEM = """あなたは大学の授業資料の表記を確かめる専門家です。資料の本文に書かれている文字列だけを、そのまま写します。"""

SURFACE_USER = """授業「{course}」の第{lecture}回の資料です（ページ番号付き）。
次の概念は、この名前のままでは資料の本文に見つかりませんでした。
それぞれ、この概念を指す語句を、下のページの本文からそのまま写してください。

{items}

守ること:
- 本文に書かれている文字列を 1 字も変えずに写す（言い換え・要約・表記の統一・送り仮名の変更をしない）
- 文ではなく、概念の名前になる短い語句を写す
- 本文にこの概念を指す語句が無ければ surface を空にする（作らない）
- page はその語句が書かれているページ番号

=== 資料 ===
{text}
"""

SURFACE_SCHEMA = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "surface": {"type": "string"},
                    "page": {"type": "integer"},
                },
                "required": ["index", "surface", "page"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["answers"],
    "additionalProperties": False,
}


def page_chunks(n_pages: int, size: int = CONCEPT_PAGES) -> list[tuple[int, int]]:
    """1〜n_pages を size ページ前後ずつに分ける（#101）。端数のかたまりが小さくならないよう、均等に割る。

    戻り値は (最初のページ, 最後のページ) の列（1 始まり、両端を含む）。size が 0 以下なら回ごと 1 つ。
    """
    if n_pages <= 0:
        return []
    if size <= 0 or n_pages <= size:
        return [(1, n_pages)]
    k = -(-n_pages // size)
    base, extra = divmod(n_pages, k)
    chunks, first = [], 1
    for i in range(k):
        last = first + base + (1 if i < extra else 0) - 1
        chunks.append((first, last))
        first = last + 1
    return chunks


# 表記ゆれとして無視する文字（中黒・イコール・空白・括弧・かぎ括弧・ハイフン）
_LOOSE_DROP = set("・･=＝-‐‑–—()（）[]［］{}「」『』〈〉《》【】\"'“”‘’") | {" ", "\t", "　", "\n"}
_PART_FIXES = ("名前の一部", "括弧の前", "部分一致")   # label の一部だけを本文から取る直し方
_SPACE_ONLY = {" ", "\t", "　", "\n"}
_HIRA_RE = re.compile(r"[ぁ-ゖ]")
_KANJI_RE = re.compile(r"[㐀-鿿々]")
_OKURI_INSERT = "(?:(?![をはがにでともへや])[ぁ-ゖ])?"   # 漢字の後に入ってよい平仮名 1 字（助詞は除く）


def _folded(text: str, drop: set[str]) -> tuple[str, list[int]]:
    """NFKC にし、drop の文字を除いた文字列と、その各文字が元の text の何文字目かの対応。"""
    out, idx = [], []
    for i, ch in enumerate(text):
        for c in normalize_text(ch) if ch.strip() else ch:
            if c not in drop:
                out.append(c)
                idx.append(i)
    return "".join(out), idx


_BRACKET_PAIRS = {")": "(", "]": "[", "}": "{", "」": "「", "』": "『", "〉": "〈", "》": "《", "】": "【",
                  "”": "“", "’": "‘", "）": "（", "］": "［"}
_OPENERS = set(_BRACKET_PAIRS.values())


def drop_unpaired_brackets(text: str) -> str:
    """対の合わないかぎ括弧・括弧を除く（#101。本文の範囲を切り出すと、範囲の外の相手を失った括弧が残るため）。"""
    stack, drop = [], set()
    for i, ch in enumerate(text):
        if ch in _OPENERS:
            stack.append(i)
        elif ch in _BRACKET_PAIRS:
            if stack and text[stack[-1]] == _BRACKET_PAIRS[ch]:
                stack.pop()
            else:
                drop.add(i)
    drop.update(stack)
    return "".join(ch for i, ch in enumerate(text) if i not in drop).strip()


def _unclosed(text: str) -> list[str]:
    """閉じていない開き括弧（内側が後ろ）。"""
    stack = []
    for ch in text:
        if ch in _OPENERS:
            stack.append(ch)
        elif ch in _BRACKET_PAIRS and stack and stack[-1] == _BRACKET_PAIRS[ch]:
            stack.pop()
    return stack


def _span(text: str, idx: list[int], start: int, end: int) -> str:
    """畳んだ文字列の [start, end) に当たる、元の text の部分（前後の空白と、対の合わない括弧を除く）。

    範囲の中で開いた括弧のすぐ後に閉じ括弧が続くなら、それも含める（例: 「理(ロゴス」→「理(ロゴス)」）。"""
    a, b = idx[start], idx[end - 1] + 1
    while b < len(text) and (open_ := _unclosed(text[a:b])) and _BRACKET_PAIRS.get(text[b]) == open_[-1]:
        b += 1
    return drop_unpaired_brackets(normalize_text(text[a:b]).strip())


def _okurigana_re(loose_label: str) -> re.Pattern | None:
    """送り仮名の違いを許す正規表現（例: 取り扱い ↔ 取扱い、功利性原理 ↔ 功利性の原理）。漢字を含み、
    漢字と平仮名が混ざるか漢字が 2 字以上の名前だけ。漢字・片仮名などは必ず一致させ、平仮名は省いてもよく、
    漢字の後に平仮名 1 字が入ってもよい。"""
    n_kanji = len(_KANJI_RE.findall(loose_label))
    if not n_kanji or (n_kanji < 2 and not _HIRA_RE.search(loose_label)):
        return None
    parts = []
    for i, ch in enumerate(loose_label):
        last = i == len(loose_label) - 1
        if _HIRA_RE.fullmatch(ch):
            parts.append(re.escape(ch) + "?")
        else:
            # 語の終わりには足さない。助詞（を・は・が など）は別の語をつなぐので入れない（「の」は許す）
            parts.append(re.escape(ch) + (_OKURI_INSERT if _KANJI_RE.fullmatch(ch) and not last else ""))
    return re.compile("".join(parts))


def _person_parts(label: str) -> list[str]:
    head = normalize_text(label).split("(")[0]
    return sorted({_squash(x) for x in _NAME_SPLIT_RE.split(head) if len(_squash(x)) >= 2}, key=len, reverse=True)


def find_surface(label: str, kind: str, page_texts: dict[int, str], person_kinds: set[str],
                 prefer: list[int] = ()) -> tuple[str, str, int] | None:
    """label の、本文での表記を探す（#101）。見つかれば (本文の表記, 直し方, ページ)、無ければ None。

    直し方の順: そのまま → 表記ゆれ（中黒・空白・括弧など）→ 送り仮名 → 名前の一部（人物の種類）→ 括弧の前 →
    部分一致（label の 2/3 以上の長さで本文に含まれる最長の部分）。同じ直し方なら prefer のページを先に見る。
    """
    pages = sorted(page_texts, key=lambda p: (p not in prefer, p))
    loose_label, _ = _folded(label, _LOOSE_DROP)
    if not loose_label:
        return None
    head = normalize_text(label).split("(")[0]
    loose_head, _ = _folded(head, _LOOSE_DROP)
    okuri = _okurigana_re(loose_label)
    folded = {p: _folded(page_texts[p], _LOOSE_DROP) for p in pages}

    exact = _squash(label)
    for p in pages:
        s, idx = _folded(page_texts[p], _SPACE_ONLY)
        i = s.find(exact) if exact else -1
        if i >= 0:
            return _span(page_texts[p], idx, i, i + len(exact)), "そのまま", p
    for p in pages:
        s, idx = folded[p]
        i = s.find(loose_label)
        if i >= 0:
            return _span(page_texts[p], idx, i, i + len(loose_label)), "表記ゆれ", p
    if okuri:
        for p in pages:
            s, idx = folded[p]
            m = okuri.search(s)
            if m:
                return _span(page_texts[p], idx, m.start(), m.end()), "送り仮名", p
    if kind in person_kinds:
        for part in _person_parts(label):
            if part == loose_label:
                continue
            for p in pages:
                s, idx = folded[p]
                i = s.find(part)
                if i >= 0:
                    return _span(page_texts[p], idx, i, i + len(part)), "名前の一部", p
    if loose_head and loose_head != loose_label:
        for p in pages:
            s, idx = folded[p]
            i = s.find(loose_head)
            if i >= 0:
                return _span(page_texts[p], idx, i, i + len(loose_head)), "括弧の前", p
    n = len(loose_label)
    min_len = max(2, -(-2 * n // 3))
    for length in range(n - 1, min_len - 1, -1):
        for start in range(0, n - length + 1):
            sub = loose_label[start:start + length]
            for p in pages:
                s, idx = folded[p]
                i = s.find(sub)
                if i >= 0:
                    return _span(page_texts[p], idx, i, i + length), "部分一致", p
    return None


def pages_with_text(surface: str, page_texts: dict[int, str]) -> list[int]:
    """表記（空白を除いて比べる）が本文に書かれているページ。"""
    s = _squash(surface)
    return [p for p in sorted(page_texts) if s and s in _squash(page_texts[p])]


def _chunk_as_prompt_text(lec: Lecture, pages: list[int]) -> str:
    parts = [f"=== 第{lec.number}回 ==="]
    for i in pages:
        parts.append(f"[第{lec.number}回 p.{i}] {lec.pages[i - 1]}")
    return "\n".join(parts)


def _direction_text(rel: dict) -> str:
    """関係ラベルの向き（from 側・to 側の種類）。旧形式の vocab.json には無いので空にする。"""
    f, t = rel.get("from_kinds") or [], rel.get("to_kinds") or []
    if not f and not t:
        return ""
    return f"【向き: from は {'・'.join(f) or '指定なし'}、to は {'・'.join(t) or '指定なし'}】"


# 端の種類が合わない関係の扱い（#49）。"other" はラベルを「その他」にして残す、"drop" は関係を落とす。
# 2 つを比べるために切り替えられるようにしている。run_extraction の引数で渡すか、環境変数で既定を変える
END_KIND_MISMATCH_POLICIES = ("other", "drop")
END_KIND_MISMATCH_DEFAULT = os.environ.get("QUIZMAP_END_KIND_MISMATCH", "other")


def check_end_kinds(f: str, t: str, label: str, concepts: dict[str, dict],
                    rel_by_label: dict[str, dict]) -> tuple[str, str, str, str | None]:
    """関係の両端の概念の種類が、ラベルの from_kinds・to_kinds に合うか（Issue #49）。LLM は呼ばない。

    合えばそのまま。合わなければ逆向きで合うかを見て、合えば from と to を入れ替える（"swapped"）。
    それでも合わなければラベルを「その他」にする（"other"）。関係そのものは資料にあるかもしれないので落とさない。
    from_kinds・to_kinds が空のラベル（旧形式の vocab）と「その他」は照合しない。
    戻り値: (from, to, label, 直した内容 または None)
    """
    rel = rel_by_label.get(label)
    if label == OTHER_LABEL or rel is None:
        return f, t, label, None
    fk, tk = set(rel.get("from_kinds") or []), set(rel.get("to_kinds") or [])
    if not fk or not tk:
        return f, t, label, None
    kf, kt = concepts[f]["kind"], concepts[t]["kind"]
    if kf in fk and kt in tk:
        return f, t, label, None
    if kt in fk and kf in tk:
        return t, f, label, "swapped"
    return f, t, OTHER_LABEL, "other"


CHUNK_OVERLAP = int(os.environ.get("QUIZMAP_CHUNK_OVERLAP", "1"))      # 前のかたまりと重ねるページ数
EXTRACT_WORKERS = int(os.environ.get("QUIZMAP_EXTRACT_WORKERS", "4"))  # かたまりを並列に呼ぶ本数
_MERGE_DROP = _SPACE_ONLY | set("・･=＝")


def extraction_chunks(n_pages: int, size: int = CONCEPT_PAGES, overlap: int = CHUNK_OVERLAP) -> list[tuple[int, int]]:
    """抽出のページの分け方（#101・#107）。page_chunks の各かたまりの前に、前のかたまりの最後の overlap ページを
    重ねる（境目をまたいで書かれた関係を失わないため。重なりで同じ概念・関係が 2 度出ても、統合と同じ対の整理でまとまる）。"""
    return [(max(1, a - overlap) if i else a, b) for i, (a, b) in enumerate(page_chunks(n_pages, size))]


def merge_key(label: str) -> str:
    """概念を同じものとしてまとめる鍵（#107）: NFKC にし、空白・中黒・イコールの違いを無視する。"""
    return _folded(label, _MERGE_DROP)[0]


def extract_graph(client, model: str, course: str, vocab: dict, lectures: list[Lecture], log=print,
                  step=lambda name: None, pages_per_call: int = CONCEPT_PAGES, overlap: int = CHUNK_OVERLAP,
                  workers: int = EXTRACT_WORKERS, end_kind_mismatch: str = END_KIND_MISMATCH_DEFAULT):
    """概念と関係を、かたまり（数ページ）ごとに 1 回の呼び出しでまとめて取る（#107）。かたまりは並列に呼ぶ。

    - 地図に出す名前は本文の表記そのもの（#101）。本文に無い label は find_surface で直し、それでも無いものは
      回ごとに 1 回 LLM に本文から写させる（これも並列）。それでも無いものは地図に載せず記録に残す。
    - 概念の統合と id はコードで決める: 本文の表記を merge_key でまとめ、最初に出たかたまり（回・ページの順）の
      id の案（ローマ字）から振る。
    - 関係の端は応答の中の label で指し、統合後の id に付け替える。そのかたまりで挙げられていない名前でも、
      ほかのかたまりで挙がった概念の名前がそのかたまりの本文にあれば、その概念につなぐ（#106 の工夫）。
    戻り値: (概念, 関係, vocab の種類に当てはまらなかった概念, 記録, 落とした数, 直した数)
    """
    if end_kind_mismatch not in END_KIND_MISMATCH_POLICIES:
        raise ValueError(f"end_kind_mismatch は {END_KIND_MISMATCH_POLICIES} のどれか: {end_kind_mismatch!r}")
    lang = language_of(vocab, (page for lec in lectures for page in lec.pages))
    system = EXTRACT_SYSTEM.replace("{language_rule}", language_rule(lang))
    kinds_text = "\n".join(
        f"- {k['name']}{'【出典の文献】' if k.get('cited_work') is True else ''}: {k['definition']}"
        f"（例: {'、'.join(kind_examples(k))}）" for k in vocab["kinds"]
    )
    rel_text = "\n".join(
        f"- {r['label']}: {r['definition']}{_direction_text(r)}（例: {r['example']}）"
        for r in vocab["relations"] if r["label"] != OTHER_LABEL
    )
    kind_names = {k["name"] for k in vocab["kinds"]}
    person_kinds = {k["name"] for k in vocab["kinds"] if k.get("person") is True}
    labels = {r["label"] for r in vocab["relations"]} | {OTHER_LABEL}
    rel_by_label = {r["label"]: r for r in vocab["relations"]}

    jobs = [(lec, first, last) for lec in lectures
            for first, last in extraction_chunks(lec.page_count, pages_per_call, overlap)]
    n_jobs_of = collections.Counter(lec.number for lec, _, _ in jobs)

    def one(job):
        lec, first, last = job
        return call_json(
            client, model=model, system=system, schema=EXTRACT_SCHEMA,
            user=EXTRACT_USER.format(
                course=course, lecture=lec.number, first=first, last=last, kinds=kinds_text,
                relations=rel_text, other=OTHER_LABEL, language=language_name(lang),
                text=_chunk_as_prompt_text(lec, list(range(first, last + 1))),
            ),
        )

    # 1) かたまりを並列に呼ぶ。結果はかたまりの順（回・ページの順）に並べ直して使う
    step("concepts")
    results: list[dict | None] = [None] * len(jobs)
    done_of = collections.Counter()
    listed_of = collections.Counter()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = {ex.submit(one, job): i for i, job in enumerate(jobs)}
        for fut in as_completed(futures):
            i = futures[fut]
            results[i] = fut.result()
            lec, first, last = jobs[i]
            done_of[lec.number] += 1
            listed_of[lec.number] += len(results[i]["concepts"])
            log(f"抽出 第{lec.number}回 p.{first}-{last}: 概念 {len(results[i]['concepts'])} 件・"
                f"関係 {len(results[i]['relations'])} 件")
            if done_of[lec.number] == n_jobs_of[lec.number]:
                log(f"概念 第{lec.number}回: かたまり {n_jobs_of[lec.number]} 個から {listed_of[lec.number]} 件")

    # 2) 挙げられた概念ごとに、本文の表記を決める
    unmatched_kind: list[dict] = []
    mentions: list[dict] = []
    for i, ((lec, first, last), data) in enumerate(zip(jobs, results)):
        chunk_texts = {p: lec.pages[p - 1] for p in range(first, last + 1)}
        for c in data["concepts"]:
            label = c["label"].strip()
            if not label:
                continue
            kind = c["kind"].strip()
            if kind != OTHER_LABEL and kind not in kind_names:
                # vocab にも「その他」にもない kind を付けてきた。落とさず「その他」に寄せ、件数と例を報告する
                if all(u["label"] != label for u in unmatched_kind):
                    unmatched_kind.append({"label": label, "kind_given": kind, "lecture": lec.number})
                kind = OTHER_LABEL
            claimed = sorted({p for p in c["pages"] if first <= p <= last})
            found = find_surface(label, kind, chunk_texts, person_kinds, claimed)
            mentions.append({"job": i, "label": label, "kind": kind, "id": c["id"],
                             "definition": c["definition"].strip(), "claimed": claimed,
                             "surface": found[0] if found else None, "how": found[1] if found else None,
                             "page": found[2] if found else None})
    # 名前の一部だけを取る直し方で、別の名前で挙がった概念と同じ表記になったものは、統合すると概念を失うので聞き直す
    strong = {merge_key(m["surface"]) for m in mentions if m["surface"] and m["how"] not in _PART_FIXES}
    for m in mentions:
        if m["surface"] and m["how"] in _PART_FIXES and merge_key(m["surface"]) in strong:
            m["surface"] = m["how"] = m["page"] = None

    # 3) 本文の表記が見つからないものを、回ごとに 1 回だけ聞き直す（並列）
    pending: dict[int, list[dict]] = collections.defaultdict(list)
    for m in mentions:
        if m["surface"] is None:
            lec = jobs[m["job"]][0]
            if all(merge_key(x["label"]) != merge_key(m["label"]) for x in pending[lec.number]):
                pending[lec.number].append(m)
            else:
                m["dup_of_pending"] = True
    lec_by_no = {lec.number: lec for lec in lectures}
    info = {"chunks": len(jobs), "calls": len(jobs), "pages_per_call": pages_per_call, "overlap": overlap,
            "workers": workers, "surface_ask_calls": 0, "surface_asked": 0, "surface_ask_resolved": 0}

    def ask(lecture_no: int):
        lec, items = lec_by_no[lecture_no], pending[lecture_no]
        shown = sorted({p for m in items for p in range(jobs[m["job"]][1], jobs[m["job"]][2] + 1)})
        lines = "\n".join(
            f"[{k}] {m['label']}（{m['kind']}）: {m['definition']}"
            f"（挙げたページ: {', '.join(f'p.{p}' for p in m['claimed']) or 'なし'}）" for k, m in enumerate(items))
        data = call_json(client, model=model, system=SURFACE_SYSTEM, schema=SURFACE_SCHEMA,
                         user=SURFACE_USER.format(course=course, lecture=lecture_no, items=lines,
                                                  text=_chunk_as_prompt_text(lec, shown)))
        return lecture_no, shown, data

    asked = {n: items for n, items in pending.items() if items}
    if asked:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            answers_of = list(ex.map(ask, sorted(asked)))
        for lecture_no, shown, data in answers_of:
            lec, items = lec_by_no[lecture_no], asked[lecture_no]
            info["surface_ask_calls"] += 1
            info["surface_asked"] += len(items)
            answers = {a["index"]: a for a in data["answers"] if 0 <= a["index"] < len(items)}
            shown_texts = {p: lec.pages[p - 1] for p in shown}
            resolved = 0
            for k, m in enumerate(items):
                surface = ((answers.get(k) or {}).get("surface") or "").strip()
                m["asked_surface"] = surface
                pages = pages_with_text(surface, shown_texts) if surface else []
                if not pages:
                    continue
                s, idx = _folded(lec.pages[pages[0] - 1], _SPACE_ONLY)
                j = s.find(_squash(surface))
                m["surface"] = _span(lec.pages[pages[0] - 1], idx, j, j + len(_squash(surface))) if j >= 0 else surface
                m["how"], m["page"], m["ask_pages"] = "聞き直し", pages[0], pages
                resolved += 1
            info["surface_ask_resolved"] += resolved
            log(f"表記の聞き直し 第{lecture_no}回: {len(items)} 件 → 本文で確かめられた {resolved}")
    # 同じ回で同じ名前の聞き直しに回したものは、代表の答えに従う
    rep = {(jobs[m["job"]][0].number, merge_key(m["label"])): m for ms in pending.values() for m in ms}
    for m in mentions:
        if m.get("dup_of_pending"):
            r = rep[(jobs[m["job"]][0].number, merge_key(m["label"]))]
            if r["surface"]:
                m["surface"], m["how"], m["page"] = r["surface"], r["how"], r["page"]

    # 4) 本文の表記でまとめ、id を振る（かたまりの順）
    concepts: dict[str, dict] = {}
    by_key: dict[str, str] = {}
    fixes: list[dict] = []
    without_surface: list[dict] = []
    local: list[dict[str, str]] = [{} for _ in jobs]     # かたまりごと: 応答の label → 概念 id
    first_label_of: dict[str, set[str]] = collections.defaultdict(set)
    new_of = collections.Counter()
    for m in mentions:
        lec, first, last = jobs[m["job"]]
        if not m["surface"]:
            if not m.get("dup_of_pending") and all(w["label"] != m["label"] or w["lecture"] != lec.number
                                                    for w in without_surface):
                without_surface.append({"label": m["label"], "kind": m["kind"], "definition": m["definition"],
                                        "lecture": lec.number, "pages": m["claimed"],
                                        "asked_surface": m.get("asked_surface", "")})
            continue
        key = merge_key(m["surface"])
        cid = by_key.get(key)
        if m["how"] != "そのまま":
            fixes.append({"from": m["label"], "to": m["surface"], "how": m["how"], "lecture": lec.number,
                          "page": m["page"], "merged_into": cid})
        if cid is None:
            cid = clean_id(m["id"], f"c{len(concepts) + 1:03d}")
            while cid in concepts:            # 別の名前なのに id が衝突した
                cid += "_"
            concepts[cid] = {"id": cid, "label": m["surface"], "kind": m["kind"], "definition": m["definition"],
                             "sources": [], "lectures": []}
            by_key[key] = cid
            new_of[lec.number] += 1
        first_label_of[cid].add(m["surface"])
        chunk_texts = m.get("ask_pages") and {p: lec.pages[p - 1] for p in m["ask_pages"]} \
            or {p: lec.pages[p - 1] for p in range(first, last + 1)}
        pages = pages_with_text(m["surface"], chunk_texts)
        for p in [p for p in pages if p in m["claimed"]] or pages:
            src = {"lecture": lec.number, "page": p}
            if src not in concepts[cid]["sources"]:
                concepts[cid]["sources"].append(src)
        if lec.number not in concepts[cid]["lectures"]:
            concepts[cid]["lectures"].append(lec.number)
        local[m["job"]][m["label"]] = cid
        local[m["job"]].setdefault(m["surface"], cid)
    for c in concepts.values():
        c["sources"].sort(key=lambda s: (s["lecture"], s["page"]))
        c["lectures"].sort()
    n_mentions = sum(1 for m in mentions if m["surface"])
    info.update({
        "mentions": n_mentions,
        "merged_mentions": n_mentions - len(concepts),                       # 統合でまとめた挙げ方の数
        "merged_variants": sum(len(v) - 1 for v in first_label_of.values()),  # 表記の違い（空白・中黒）をまとめた数
        "listed_by_lecture": dict(sorted(listed_of.items())),
        "new_by_lecture": {lec.number: new_of[lec.number] for lec in lectures},
        "label_fixes": fixes,
        "label_fix_by_how": dict(collections.Counter(f["how"] for f in fixes)),
        "without_surface": without_surface,
    })
    log(f"概念の統合: 挙げ方 {n_mentions} 件 → 概念 {len(concepts)}（まとめた {info['merged_mentions']}、"
        f"うち表記の違い {info['merged_variants']}）。回ごとの新規: "
        + " ".join(f"{n}:{k}" for n, k in info["new_by_lecture"].items()))
    if fixes:
        log(f"名前を本文の表記に合わせた {len(fixes)} 件（"
            + ", ".join(f"{k} {v}" for k, v in info["label_fix_by_how"].items()) + "）")
    if without_surface:
        log(f"資料の表記が見つからない概念 {len(without_surface)} 件（地図には載せず graph に記録）: "
            + " / ".join(m["label"] for m in without_surface[:SURFACE_EXAMPLES]))

    # 5) 関係の端を統合後の id に付け替え、これまでと同じ照合をかける
    step("relations")
    unresolved = {merge_key(w["label"]) for w in without_surface}
    adjusted = collections.Counter()
    dropped = collections.Counter()
    candidates: list[dict] = []
    kept_of = collections.Counter()

    def end_id(name: str, i: int, chunk_texts: dict[int, str]) -> str | None:
        name = name.strip()
        loc = local[i]
        if name in loc:
            return loc[name]
        key = merge_key(name)
        for lab, cid in loc.items():
            if merge_key(lab) == key:
                return cid
        cid = by_key.get(key)                 # ほかのかたまりで挙がった概念で、名前がこのかたまりの本文にあるもの
        if cid and pages_with_text(concepts[cid]["label"], chunk_texts):
            return cid
        return None

    for i, ((lec, first, last), data) in enumerate(zip(jobs, results)):
        chunk_texts = {p: lec.pages[p - 1] for p in range(first, last + 1)}
        for r in data["relations"]:
            f, t = end_id(r["from"], i, chunk_texts), end_id(r["to"], i, chunk_texts)
            label = r["label"].strip()
            if f is None or t is None:
                names = [r["from"], r["to"]]
                dropped["端が資料の表記に無い" if any(merge_key(n) in unresolved for n in names)
                        else "存在しない概念"] += 1
                continue
            if f == t:
                dropped["自己ループ"] += 1
                continue
            if label not in labels:
                dropped["vocab にないラベル→その他"] += 1
                label = OTHER_LABEL
            f, t, label, fix = check_end_kinds(f, t, label, concepts, rel_by_label)
            if fix == "swapped":
                adjusted["向きを直した"] += 1
            elif fix == "other" and end_kind_mismatch == "drop":
                dropped["端の種類が合わない"] += 1
                continue
            elif fix == "other":
                dropped["端の種類が合わない→その他"] += 1
            if not first <= r["page"] <= last:
                dropped["ページ番号が範囲外"] += 1
                continue
            page = r["page"]
            ends = [concepts[f]["label"], concepts[t]["label"]]
            reason = check_evidence(r.get("evidence", ""), ends, lec.pages[page - 1])
            if reason:
                dropped[reason] += 1
                continue
            # evidence は検証と同点の判定に使うだけで保存しない（講義資料の本文を graph.json に増やさない）
            candidates.append({"from": f, "to": t, "label": label,
                               "source": {"lecture": lec.number, "page": page},
                               "_rank": evidence_rank(r.get("evidence", ""), ends, lec.pages[page - 1])})
            kept_of[lec.number] += 1
    for lec in lectures:
        log(f"関係 第{lec.number}回: 候補 {kept_of[lec.number]}")
    relations, merged = resolve_pair_conflicts(candidates)
    dropped.update(merged)
    log("同じ概念対の整理: " + (", ".join(f"{k} {v}" for k, v in merged.items()) or "なし")
        + f" → 関係 {len(relations)}")
    if adjusted or dropped.get("端の種類が合わない→その他") or dropped.get("端の種類が合わない"):
        log(f"端の種類の照合（{end_kind_mismatch}）: 向きを直した {adjusted['向きを直した']}、"
            f"「その他」にした {dropped.get('端の種類が合わない→その他', 0)}、落とした {dropped.get('端の種類が合わない', 0)}")
    info["calls"] = len(jobs) + info["surface_ask_calls"]
    log(f"抽出の呼び出し {info['calls']} 回（かたまり {len(jobs)} 個、{pages_per_call} ページ前後ずつ・{overlap} ページ重ねる・"
        f"並列 {workers}、表記の聞き直し {info['surface_ask_calls']} 回）")
    return concepts, relations, unmatched_kind, info, dict(dropped), dict(adjusted)


def resolve_pair_conflicts(candidates: list[dict]) -> tuple[list[dict], collections.Counter]:
    """1 つの概念対（向きを問わない）に、1 つのラベルと 1 つの向きだけを残す（Issue #12）。

    回ごとに独立に抽出するため、同じ対に別のラベルや逆向きの関係が付くことがある。
    LLM は呼ばず、資料中で何度その関係が書かれたか（支持数）で決める。
    1. ラベルが 2 つ以上あれば、支持数（両向きの合計）の多いラベルを残す。
       「その他」も同じように比べる。語彙に合うラベルが無いときは「その他」が正しいことがあるため
    2. そのラベルが両向きにあれば、支持数の多い向きを残す
       （対立のように向きの無いラベルでも、両向きは同じ関係の重複なので 1 本にしてよい）
    支持数が同点なら、根拠の確かさ（候補の "_rank"。evidence_rank の値）の大きいものを残す。
    それも同点なら先に出たもの（若い回）を残す。残した関係の source は、その (from, to, label) が最初に出た箇所。
    candidates は出現順に並んでいること。完全に同じ (from, to, label) も支持数として数える。
    "_rank" は判定に使うだけで、戻り値には残さない。
    """
    dropped = collections.Counter()
    first: dict[tuple, dict] = {}                     # (from, to, label) -> 最初に出た関係
    support = collections.Counter()                   # (from, to, label) -> 出た回数
    order: dict[tuple, int] = {}                      # (from, to, label) -> 最初に出た位置
    rank: dict[tuple, tuple] = {}                     # (from, to, label) -> 根拠の確かさの最大
    for i, r in enumerate(candidates):
        key = (r["from"], r["to"], r["label"])
        support[key] += 1
        rank[key] = max(rank.get(key, ()), tuple(r.get("_rank", ())))
        if key not in first:
            first[key], order[key] = r, i
        else:
            dropped["重複"] += 1

    by_pair: dict[frozenset, list[tuple]] = collections.defaultdict(list)
    for key in first:
        by_pair[frozenset(key[:2])].append(key)

    kept: list[tuple] = []
    for keys in by_pair.values():
        if len(keys) > 1:
            label_support = collections.Counter()
            label_order: dict[str, int] = {}
            label_rank: dict[str, tuple] = {}
            for k in keys:
                label_support[k[2]] += support[k]
                label_order[k[2]] = min(label_order.get(k[2], order[k]), order[k])
                label_rank[k[2]] = max(label_rank.get(k[2], ()), rank[k])
            # 支持数の多い順 → 根拠の確かな順 → 先に出た順。rank は大きいほうを先にするので符号を反転して比べる
            best_label = min(label_support, key=lambda l: (-label_support[l], [-x for x in label_rank[l]], label_order[l]))
            best = min((k for k in keys if k[2] == best_label),
                       key=lambda k: (-support[k], [-x for x in rank[k]], order[k]))
            dropped["同じ対の食い違い"] += len(keys) - 1
            keys = [best]
        kept.append(keys[0])

    kept.sort(key=order.__getitem__)
    return [{k2: v for k2, v in first[k].items() if k2 != "_rank"} for k in kept], dropped


def remove_cited_works(concepts: dict[str, dict], vocab: dict) -> tuple[dict[str, dict], list[str]]:
    """cited_work が true の種類（資料が出典・参考として挙げた文献・作品）の概念を落とす（#50）。

    種類の名前ではなく vocab の旗で決めるので、分野に依らない。関係の抽出の前に落とすので、
    その概念を端にする関係は作られない（関係ごと落ちる）。cited_work の無い古い vocab.json では何も落とさない。
    戻り値: (残った概念, 落とした種類の名前)
    """
    cited = {k["name"] for k in vocab["kinds"] if k.get("cited_work") is True}
    if not cited:
        return concepts, []
    return {cid: c for cid, c in concepts.items() if c["kind"] not in cited}, sorted(cited)


def cited_work_labels(concepts: dict[str, dict], vocab: dict) -> list[str]:
    """remove_cited_works で落とす概念の label（#69。巻き込みを後から確かめられるように全件残す）。"""
    cited = {k["name"] for k in vocab["kinds"] if k.get("cited_work") is True}
    return [c["label"] for c in concepts.values() if c["kind"] in cited]


# ------------------------------------------------------------------ 関係の補完（docs/rag-design.md、#78）
#
# 利用者が決めた方針: 地図の概念はアップロードした資料にあるものだけ。外部の資料（Wikipedia）は
# 概念どうしの関係を確かめるためだけに使う。なので両端は既存の概念 id に限り、新しい概念は作らない。

COMPLETION_SYSTEM = """あなたは大学の授業資料の概念どうしに関係があるかを、資料のページと百科事典（Wikipedia）の記事の記述で確かめる専門家です。
関係の両端には与えられた概念対の id だけを使い、関係ラベルには与えられたラベルだけを使います。"""

COMPLETION_USER = """授業「{course}」の概念の対について、関係があるかを判定してください。

関係ラベル（これ以外は使わない）:
{relations}

=== 資料のページ（[第N回 pM] の形で示す） ===
{pages}

=== Wikipedia の記事の冒頭（[W番号] の形で示す） ===
{wiki}

=== 判定する概念の対 ===
{pairs}

各対について 1 件ずつ答える。資料の根拠と Wikipedia の根拠は、それぞれ別に探して、両方の欄に書く。
- from・to・label: 二つの概念の関係を、上のラベルの定義で表す。from と to は対の 2 つの id で、ラベルの定義が決める向きに従う。
  from と to は、そのラベルの例（「A → B」の A が from、B が to）と同じ向きで書く。逆向きの関係なら入れ替える。
  ほかの id は使わず、新しい概念も作らない。関係が無い、またはどのラベルにも当てはまらなければ label を空にする
- 一方が他方の定義の言い換え（「A とは B のこと」）であるだけの対は、ほかのラベルに押し込まない。それを表すラベルが無ければ label を空にする
- 自信が無ければ label を空にする（関係を足さない側に倒す）
- 資料の根拠: 対に付けた資料のページに、二つの概念を結びつける記述があれば slide_found を true にし、lecture と page にそのページを、
  slide_evidence にその箇所をページ本文からそのまま 40 字以内で抜き出す。引用には二つの概念の名前を両方入れる
- Wikipedia の根拠: 資料に明示が無くても、対に付けた Wikipedia の記事の本文に二つの概念を結びつける記述があれば、external_found を true にする。
  wiki にその記事の番号を、wiki_evidence にその箇所を記事の本文からそのまま 80 字以内で抜き出す。
  引用には二つの概念の名前を両方入れる。資料の根拠があるときも、Wikipedia の根拠があれば書く
- 引用が、対の概念ではない別の概念についての記述なら、根拠にしない
- 判断の基準: 根拠にしてよいのは、資料のページか記事の本文にそのまま書かれた記述だけ（引用は必須）。あなた自身の知識だけで関係を決めない。
  同じページや同じ記事に二つの名前が出てくるだけでは根拠にしない。二つの概念の間柄を述べた記述（一方を主語に、他方を目的語や補語にした文など）が要る
- 使わない欄は、文字列なら空、数なら 0、真偽なら false にする
"""

COMPLETION_SCHEMA = {
    "type": "object",
    "properties": {
        "judgements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pair": {"type": "integer"},
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "label": {"type": "string"},
                    "slide_found": {"type": "boolean"},
                    "lecture": {"type": "integer"},
                    "page": {"type": "integer"},
                    "slide_evidence": {"type": "string"},
                    "external_found": {"type": "boolean"},
                    "wiki": {"type": "integer"},
                    "wiki_evidence": {"type": "string"},
                },
                "required": ["pair", "from", "to", "label", "slide_found", "lecture", "page", "slide_evidence",
                             "external_found", "wiki", "wiki_evidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["judgements"],
    "additionalProperties": False,
}

COMPLETION_MAX_PAIRS = int(os.environ.get("QUIZMAP_COMPLETION_MAX_PAIRS", "60"))
COMPLETION_BATCH = 12          # 1 回の呼び出しで判定する対の数
COMPLETION_PAGES_PER_PAIR = 3  # 1 つの対に付ける資料のページの数
COMPLETION_SEARCH_K = 3        # どの関係にもつながらない概念について、索引から取るページの数
PAGE_CHARS = 1500              # プロンプトに載せるページ本文の長さの上限
WIKI_CHARS = 800               # プロンプトに載せる Wikipedia の冒頭の長さの上限


def _pages_of(c: dict) -> set[tuple[int, int]]:
    return {(s["lecture"], s["page"]) for s in c.get("sources", [])}


def completion_candidates(concepts: dict[str, dict], relations: list[dict], page_text: dict[tuple[int, int], str],
                          index, client, max_pairs: int = COMPLETION_MAX_PAIRS,
                          search_k: int = COMPLETION_SEARCH_K) -> tuple[list[dict], dict]:
    """関係を補う候補の対を選ぶ。LLM は呼ばない（索引の検索は埋め込みを 1 回呼ぶ）。

    1. 同じページに一緒に出てくるのに関係の無い対
    2. どの関係にもつながらない概念は、索引で近いページを探し（search_many で全部を 1 回の埋め込みにまとめる）、そのページに出てくる概念と対にする
       （索引が無ければ 1 だけ）
    どの関係にもつながらない概念を含む対を先に、次に一緒に出るページの多い対を先にし、max_pairs 本で切る。
    戻り値: (候補 [{"a", "b", "pages": [(lecture, page)], "page_info": [{"lecture", "page", "score", "via"}],
              "isolated": bool}], 数の記録)。via は "shared"（両方が出るページ）か "search"（索引で見つけたページ）
    """
    related = {frozenset((r["from"], r["to"])) for r in relations}
    linked = {x for r in relations for x in (r["from"], r["to"])}
    isolated = [cid for cid in concepts if cid not in linked]

    on_page: dict[tuple[int, int], list[str]] = collections.defaultdict(list)
    for cid, c in concepts.items():
        for p in sorted(_pages_of(c)):
            on_page[p].append(cid)

    pairs: dict[frozenset, dict] = {}

    def add(a: str, b: str, page: tuple[int, int], shared: bool, score: float | None = None):
        key = frozenset((a, b))
        if a == b or key in related or page not in page_text:
            return
        entry = pairs.setdefault(key, {"a": min(a, b), "b": max(a, b), "pages": [], "page_info": [], "shared": 0,
                                       "isolated": a not in linked or b not in linked})
        if page not in entry["pages"]:
            entry["pages"].append(page)
            entry["page_info"].append({"lecture": page[0], "page": page[1], "score": score,
                                       "via": "shared" if shared else "search"})
            entry["shared"] += shared

    for page, ids in on_page.items():
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                add(a, b, page, shared=True)

    searches = 0
    if index is not None and isolated:
        # 全部の概念名を search_many の 1 回の埋め込みでまとめて引く（#84）。exclude は全問い合わせで共通なので、
        # 概念ごとの「自分のページ」は渡さず、その分だけ多めに取ってからここで除く
        own = {cid: _pages_of(concepts[cid]) for cid in isolated}
        k = search_k + max(len(v) for v in own.values())
        results = index.search_many(client, [concepts[cid]["label"] for cid in isolated], k=k)
        searches = len(isolated)
        for cid, found in zip(isolated, results):
            hits = [h for h in found if (h["lecture"], h["page"]) not in own[cid]][:search_k]
            for h in hits:
                page = (h["lecture"], h["page"])
                for other in on_page.get(page, []):
                    add(cid, other, page, shared=False, score=h.get("score"))

    ranked = sorted(pairs.values(), key=lambda p: (not p["isolated"], -p["shared"], p["a"], p["b"]))
    chosen = ranked[:max_pairs]
    for p in chosen:
        p["pages"] = p["pages"][:COMPLETION_PAGES_PER_PAIR]
        p["page_info"] = p["page_info"][:COMPLETION_PAGES_PER_PAIR]
        p.pop("shared")
    return chosen, {"candidates": len(pairs), "judged": len(chosen), "isolated_before": len(isolated),
                    "searches": searches}


def _wiki_text(article: dict) -> str:
    return normalize_text(article.get("extract", ""))[:WIKI_CHARS]


def check_external_evidence(evidence: str, end_labels: list[str], article: dict) -> str | None:
    """Wikipedia で確かめた関係の根拠を照合する。落とす理由、または None を返す。

    evidence が記事の冒頭（プロンプトに載せた範囲）にそのまま有ること、
    両端の概念名がどちらも記事の題名か冒頭に出てくること（片方の記事に自分の名前が出るだけでは足りない）。
    """
    text = _squash(_wiki_text(article))
    ev = _squash(evidence)
    if not ev or ev not in text:
        return "evidence が記事に無い"
    where = _squash(article.get("title", "")) + text
    if not all(h and h in where for h in (_label_head(l) for l in end_labels)):
        return "両端の概念名が記事に無い"
    return None


_NAME_SPLIT_RE = re.compile(r"[・･=＝\s]+")


def _name_variants(concept: dict, person_kinds: set[str]) -> list[str]:
    """evidence の中で概念を見つけるための名前。括弧の前（_label_head）。人物の種類なら「・」などで区切った各部分も
    （「イマヌエル・カント」は「カント」でもよい。区切りの無い名前は全体だけ）。2 字未満の部分は使わない。"""
    names = [_label_head(concept["label"])]
    if concept.get("kind") in person_kinds:
        head = normalize_text(concept["label"]).split("(")[0]
        names += [_squash(x) for x in _NAME_SPLIT_RE.split(head) if len(_squash(x)) >= 2]
    return [n for n in dict.fromkeys(names) if n]


def check_ends_in_evidence(evidence: str, ends: list[dict], person_kinds: set[str]) -> str | None:
    """補完の関係の evidence に、両端の概念名が両方あるか（#92）。無ければ落とす理由を返す。

    別の概念についての記述（片方の名前しか無い）を根拠にした関係を足さないため。迷うものは落とす側に倒す。
    """
    ev = _squash(evidence)
    if all(any(n in ev for n in _name_variants(c, person_kinds)) for c in ends):
        return None
    return "evidence に両端が無い"


def complete_relations(client, model, vocab, graph, pages, index, wiki, log=print,
                       max_pairs: int = COMPLETION_MAX_PAIRS) -> dict:
    """関係の補完（docs/rag-design.md）。graph に関係を足した新しい graph を返す（ファイルは書かない）。

    両端は既存の概念 id だけ（新しい概念は作らない）。足す関係には origin を付ける。
      "slide":    スライドの記述が根拠。source は根拠のページ、evidence はページ本文からの引用
      "external": スライドに明示が無く Wikipedia で確かめた。source は両端の概念が出てくるページ、
                  evidence は記事からの引用、evidence_url と evidence_title を付ける
    既存の関係には origin: "slide" を付ける。足した関係には added_by: "completion" も付ける（抽出の関係と見分けるため）。
    照合は抽出と同じ規則（端の種類、evidence の本文照合）を通す。
    stats.completion.pairs に候補ごとの記録を残す（eval-protocol.md §7.5）:
      {"a", "b", "pages": [{"lecture", "page", "score", "via"}], "wiki": [{"term", "title", "url"}],
       "judgement": LLM の答え（origin（slide_found・external_found から決めた主張）・label・from・to・
                    slide_found・lecture・page・slide_evidence・external_found・wiki・wiki_evidence）,
       "result": "added" / "rejected" / "none" / "no_answer",
       "reason": 落とした理由, "relation": 足した関係}
    index（SlideIndex）と wiki（Wiki）は None でもよい。そのときは使える層だけで判定する。
    """
    concepts = {c["id"]: c for c in graph["concepts"]}
    relations = [dict(r, origin=r.get("origin", "slide")) for r in graph["relations"]]
    page_text = {(p["lecture"], p["page"]): p["text"] for p in pages}
    labels = {r["label"] for r in vocab["relations"]} - {OTHER_LABEL}
    rel_by_label = {r["label"]: r for r in vocab["relations"]}
    person_kinds = {k["name"] for k in vocab["kinds"] if k.get("person") is True}

    pairs, record = completion_candidates(concepts, relations, page_text, index, client, max_pairs=max_pairs)
    log(f"関係の補完: 候補の対 {record['candidates']}（判定する {record['judged']}）、"
        f"どの関係にもつながらない概念 {record['isolated_before']}")

    articles: dict[str, dict | None] = {}
    if wiki is not None:
        for cid in sorted({x for p in pairs for x in (p["a"], p["b"])}):
            articles[cid] = wiki.lookup(concepts[cid]["label"])
    record["wiki_found"] = sum(1 for a in articles.values() if a)
    record["wiki_lookups"] = len(articles)

    pair_records = [
        {"a": p["a"], "b": p["b"], "pages": p["page_info"],
         "wiki": [{"term": concepts[cid]["label"], "title": (articles.get(cid) or {}).get("title", ""),
                   "url": (articles.get(cid) or {}).get("url", "")} for cid in (p["a"], p["b"]) if cid in articles],
         "judgement": None, "result": "no_answer", "reason": "", "relation": None}
        for p in pairs
    ]
    rel_text = "\n".join(
        f"- {r['label']}: {r['definition']}{_direction_text(r)}（例: {r['example']}）"
        for r in vocab["relations"] if r["label"] in labels
    )
    dropped = collections.Counter()
    adjusted = collections.Counter()
    added: list[dict] = []
    api_calls = 0

    for start in range(0, len(pairs), COMPLETION_BATCH):
        batch = pairs[start:start + COMPLETION_BATCH]
        page_keys = sorted({pg for p in batch for pg in p["pages"]})
        wiki_ids: list[str] = []                     # [W番号] → 概念 id（記事のある概念だけ）
        for p in batch:
            for cid in (p["a"], p["b"]):
                if articles.get(cid) and cid not in wiki_ids:
                    wiki_ids.append(cid)
        pages_block = "\n\n".join(f"[第{l}回 p{n}]\n{page_text[(l, n)][:PAGE_CHARS]}" for l, n in page_keys)
        wiki_block = "\n\n".join(
            f"[W{i + 1}] {articles[cid]['title']}\n{_wiki_text(articles[cid])}" for i, cid in enumerate(wiki_ids)
        ) or "（なし）"

        def describe(cid: str) -> str:
            c = concepts[cid]
            w = f"、記事 W{wiki_ids.index(cid) + 1}" if cid in wiki_ids else ""
            return f"{cid}（{c['label']}／種類: {c['kind']}{w}）: {c.get('definition', '')}"

        pairs_block = "\n".join(
            f"{start + i + 1}. {describe(p['a'])}\n   と {describe(p['b'])}\n"
            f"   ページ: {'、'.join(f'第{l}回 p{n}' for l, n in p['pages'])}"
            for i, p in enumerate(batch)
        )
        data = call_json(
            client, model=model, system=COMPLETION_SYSTEM, schema=COMPLETION_SCHEMA,
            user=COMPLETION_USER.format(course=vocab.get("course", ""), relations=rel_text,
                                        pages=pages_block, wiki=wiki_block, pairs=pairs_block),
        )
        api_calls += 1
        n_before = len(added)

        done: set[int] = set()

        def reject(reason: str):
            dropped[reason] += 1
            pair_records[n].update(result="rejected", reason=reason)

        for j in data["judgements"]:
            n = j["pair"] - 1
            if not start <= n < start + len(batch) or n in done:
                dropped["対の番号が無い・重複"] += 1
                continue
            done.add(n)
            p = pairs[n]
            rec = pair_records[n]
            # 資料の根拠を先に、通らなければ Wikipedia の根拠を使う（#88。LLM には両方を別の欄に書かせる）
            claimed = "slide" if j["slide_found"] else "external" if j["external_found"] else "none"
            rec["judgement"] = {"origin": claimed, **{k: j[k] for k in (
                "label", "from", "to", "slide_found", "lecture", "page", "slide_evidence",
                "external_found", "wiki", "wiki_evidence")}}
            f, t, label = j["from"].strip(), j["to"].strip(), j["label"].strip()
            if claimed == "none" or not label:
                dropped["関係なし"] += 1
                rec["result"] = "none"
                continue
            if {f, t} != {p["a"], p["b"]}:
                reject("対に無い id")
                continue
            if label not in labels:
                reject("語彙にないラベル")
                continue
            f, t, label, fix = check_end_kinds(f, t, label, concepts, rel_by_label)
            if fix == "other":
                reject("端の種類が合わない")
                continue
            if fix == "swapped":
                adjusted["向きを直した"] += 1
            ends = [concepts[f]["label"], concepts[t]["label"]]
            end_concepts = [concepts[f], concepts[t]]
            rel = {"from": f, "to": t, "label": label}
            reasons = []
            if j["slide_found"]:
                page = (j["lecture"], j["page"])
                reason = "対に付けていないページ" if page not in p["pages"] else \
                    check_evidence(j["slide_evidence"], ends, page_text[page]) or \
                    check_ends_in_evidence(j["slide_evidence"], end_concepts, person_kinds)
                if reason:
                    reasons.append(reason)
                else:
                    rel.update(source={"lecture": page[0], "page": page[1]}, origin="slide",
                               evidence=j["slide_evidence"].strip())
            if "origin" not in rel and j["external_found"]:
                cid = wiki_ids[j["wiki"] - 1] if 1 <= j["wiki"] <= len(wiki_ids) else None
                reason = ("記事の番号が無い" if cid is None else "対に付けていない記事" if cid not in (f, t)
                          else check_external_evidence(j["wiki_evidence"], ends, articles[cid])
                          or check_ends_in_evidence(j["wiki_evidence"], end_concepts, person_kinds))
                if reason:
                    reasons.append(reason)
                else:
                    article = articles[cid]
                    shared = sorted(_pages_of(concepts[f]) & _pages_of(concepts[t]))
                    lec, pg = (shared or p["pages"])[0]
                    rel.update(source={"lecture": lec, "page": pg}, origin="external",
                               evidence=j["wiki_evidence"].strip(), evidence_url=article.get("url", ""),
                               evidence_title=article.get("title", ""))
            if "origin" not in rel:
                reject(reasons[0])                      # dropped には 1 対 1 件で数える（最初の理由）
                if len(reasons) > 1:
                    rec["reason"] = " / ".join(reasons)
                continue
            if reasons:
                rec["reason"] = "資料の根拠は通らなかった: " + reasons[0]
            rel["added_by"] = "completion"
            added.append(rel)
            rec.update(result="added", relation=rel)
        missing = len(batch) - len(done)
        if missing:
            dropped["答えの無い対"] += missing
        log(f"関係の補完 {start + 1}〜{start + len(batch)}: 追加 {len(added) - n_before} 件")

    relations += added
    linked = {x for r in relations for x in (r["from"], r["to"])}
    n_other = sum(1 for r in relations if r["label"] == OTHER_LABEL)
    record.update(
        added_slide=sum(1 for r in added if r["origin"] == "slide"),
        added_external=sum(1 for r in added if r["origin"] == "external"),
        isolated_after=sum(1 for cid in concepts if cid not in linked),
        api_calls=api_calls,
        dropped=dict(dropped),
        adjusted=dict(adjusted),
        pairs=pair_records,
    )
    stats = dict(graph.get("stats", {}))
    stats.update(
        relations=len(relations),
        other_count=n_other,
        other_rate=round(n_other / len(relations), 4) if relations else 0.0,
        relations_by_label=dict(collections.Counter(r["label"] for r in relations).most_common()),
        completion=record,
    )
    log(f"関係の補完: slide {record['added_slide']} / external {record['added_external']} を追加、"
        f"どの関係にもつながらない概念 {record['isolated_before']} → {record['isolated_after']}、"
        f"LLM {api_calls} 回・索引の検索 {record['searches']} 回")
    return dict(graph, relations=relations, stats=stats)



# ------------------------------------------------------------------ 画像

def render_slides(lectures: list[Lecture], slides_dir: Path, dpi: int) -> int:
    slides_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for lec in lectures:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "p"
            subprocess.run(["pdftoppm", "-jpeg", "-r", str(dpi), str(lec.path), str(prefix)],
                           check=True, capture_output=True)
            for img in sorted(Path(tmp).glob("p-*.jpg")):
                page = int(img.stem.split("-")[-1])
                shutil.move(str(img), slides_dir / f"L{lec.number}_p{page}.jpg")
                n += 1
    return n


# ------------------------------------------------------------------ まとめて実行

def run_extraction(client, model: str, vocab: dict, lectures: list[Lecture], out_dir: Path,
                   workers: int = EXTRACT_WORKERS, dpi: int = 80, slides: bool = True, log=print,
                   step=lambda name: None, end_kind_mismatch: str = END_KIND_MISMATCH_DEFAULT) -> dict:
    """抽出して out_dir に pages.json / graph.json / slides/ を書き、graph を返す。"""
    course = vocab.get("course", "")
    total_pages = sum(l.page_count for l in lectures)
    write_json(out_dir / "pages.json", [
        {"lecture": lec.number, "page": i, "text": page}
        for lec in lectures for i, page in enumerate(lec.pages, start=1)
    ])

    concepts, relations, unmatched_kind, concept_info, dropped, adjusted = extract_graph(
        client, model, course, vocab, lectures, log=log, step=step, workers=workers,
        end_kind_mismatch=end_kind_mismatch)
    n_before = len(concepts)
    removed_labels = cited_work_labels(concepts, vocab)
    concepts, cited_kinds = remove_cited_works(concepts, vocab)
    n_cited = n_before - len(concepts)
    if cited_kinds:
        # 概念と関係を同時に取るので、落とした概念を端にする関係もここで落とす
        n_rel = len(relations)
        relations = [r for r in relations if r["from"] in concepts and r["to"] in concepts]
        if n_rel - len(relations):
            dropped["端が出典の文献"] = n_rel - len(relations)
        log(f"出典・参考の文献・作品の種類（{'・'.join(cited_kinds)}）の概念 {n_cited} 件と、それを端にする関係 "
            f"{n_rel - len(relations)} 本を落とした: " + " / ".join(removed_labels))
    if slides:
        step("slides")
        n_imgs = render_slides(lectures, out_dir / "slides", dpi)
        log(f"スライド画像 {n_imgs} 枚")

    n_other = sum(1 for r in relations if r["label"] == OTHER_LABEL)
    other_rate = n_other / len(relations) if relations else 0.0
    n_other_kind = sum(1 for c in concepts.values() if c["kind"] == OTHER_LABEL)
    other_kind_rate = n_other_kind / len(concepts) if concepts else 0.0
    label_counts = collections.Counter(r["label"] for r in relations)
    linked = {x for r in relations for x in (r["from"], r["to"])}
    n_isolated = sum(1 for cid in concepts if cid not in linked)
    kind_counts = collections.Counter(c["kind"] for c in concepts.values())
    stats = {
        "lectures": [l.number for l in lectures],
        "pages": total_pages,
        "concepts": len(concepts),
        "relations": len(relations),
        "other_count": n_other,
        "other_rate": round(other_rate, 4),
        "other_kind_count": n_other_kind,
        "other_kind_rate": round(other_kind_rate, 4),
        "unmatched_kind_count": len(unmatched_kind),
        "removed_cited_work_concepts": n_cited,
        "cited_work_kinds": cited_kinds,
        "removed_cited_work_examples": removed_labels,
        "unmatched_kind_examples": unmatched_kind[:20],
        "extract_chunks": concept_info["chunks"],
        "extract_calls": concept_info["calls"],
        "extract_pages_per_call": concept_info["pages_per_call"],
        "extract_overlap": concept_info["overlap"],
        "extract_workers": concept_info["workers"],
        "merged_mentions": concept_info["merged_mentions"],
        "merged_variants": concept_info["merged_variants"],
        "concepts_listed_by_lecture": concept_info["listed_by_lecture"],
        "new_concepts_by_lecture": concept_info["new_by_lecture"],
        "label_fixed_count": len(concept_info["label_fixes"]),
        "label_fix_by_how": concept_info["label_fix_by_how"],
        "label_fixes": concept_info["label_fixes"],
        "surface_ask_calls": concept_info["surface_ask_calls"],
        "surface_asked": concept_info["surface_asked"],
        "surface_ask_resolved": concept_info["surface_ask_resolved"],
        "without_surface_count": len(concept_info["without_surface"]),
        "without_surface_examples": concept_info["without_surface"][:SURFACE_EXAMPLES],
        "isolated_concepts": n_isolated,
        "relations_by_label": dict(label_counts.most_common()),
        "concepts_by_kind": dict(kind_counts.most_common()),
        "dropped": dropped,
        "adjusted": adjusted,
        "end_kind_mismatch": end_kind_mismatch,
        "model": model,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    graph = {
        "course": course,
        "concepts": list(concepts.values()),
        "relations": relations,
        "stats": stats,
        # 資料の表記が見つからない概念（#101）。地図の点にはしないが消さずに残す
        "concepts_without_surface": concept_info["without_surface"],
    }
    write_json(out_dir / "graph.json", graph)
    log(f"概念 {len(concepts)} / 関係 {len(relations)} / どの関係にもつながらない概念 {n_isolated} / 関係の「その他」率 {other_rate:.1%} / 種類の「その他」率 {other_kind_rate:.1%}")
    return graph
