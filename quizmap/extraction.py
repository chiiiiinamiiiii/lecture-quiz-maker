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

try:                                       # 「関連」は語彙の外の固定ラベル（#127）。common に入るまでの間は同じ値を使う
    from quizmap.common import RELATED_LABEL
except ImportError:  # pragma: no cover
    RELATED_LABEL = "関連"
from quizmap.vocab import (concept_label_kinds, kind_examples, language_name, language_of, language_rule,
                           relation_sentence)

# ------------------------------------------------------------------ 概念と関係の抽出（かたまりごとに 1 回、#107）

# 構造を表すラベル（一種・部分と全体・同一・例）の押し込みと向きの誤りを止める注意（#135・#142・#148）。
# ラベルの名前ではなく意味で書く（§7）。抽出・補完・つなぐ工程のプロンプトに同じものを入れる
STRUCTURE_RULES = """- ラベルを書く前に、ラベルの定義の from と to がそれぞれ何か（下位か上位か、部分か全体か、具体例か抽象か）を確かめ、
  書こうとしている関係の向きと合っているときだけ書く
- 「〜の一種」「下位概念」を表すラベルは、from が to の一種であるときだけ使う。部分と全体（ネットワークとその枝など）、
  並んだ別々の項目（候補として並べられた別の種類）、同じものの言い換え・同一視には使わない
- 部分と全体を表すラベルは、資料が全体をその部分から成るものとして書いているときだけ使う。同じ一覧に並んだ項目、
  ある量を求める計算に使う別の量（利益と収入・費用など）、手順とその結果には使わない
- 同じものを指す・定義・言い換えを表すラベルは、資料が二つを同じものとして書いているときだけ使う。
  一方が他方から計算されるだけのもの（1 − x、〜の確率）、総称とその中身（ある原則の集まりと個々の原則）、
  類と種、例と概念、制度とその条件には使わない
- 具体例を表すラベルは、定義で from と to のどちらが具体例かを確かめ、その向きで書く
- ラベルに【文の型】があるときは、型の {{from}} と {{to}} に二つの名前を入れた文（型「{{from}}は{{to}}の根拠である」なら
  「自律は尊厳の根拠である」）が、資料の内容として正しいかで確かめる。正しくなければ from と to を入れ替えた文で確かめ、
  それも正しくなければそのラベルを使わない
- 対応する別の量（母集団の量と標本の量、量とそれを表す記号）や、量どうしを、一種・下位概念の関係にしない"""

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
- 数式・等式そのもの、変数や記号の名前、数値の例（計算の途中や答え）、ソフト・ウェブツール・計算サイトの名前は、
  概念として挙げない。数式や記号が表す概念の名前（その式が何のモデル・法則・量か）が本文に書かれていれば、その名前を挙げる
- label は、そのページの本文に書かれている表記を、そのまま写す（言い換え・要約・表記の統一・送り仮名の変更をしない）。
  短い{language}の語句にする（文にしない）
- id は label をローマ字にした snake_case の ASCII（例: kant, gimu_ron）
- definition は資料の記述に基づいて 1〜2 文
- pages はその概念の名前が書かれているページの番号（p.{first}〜p.{last} から。複数可）。必ず 1 つ以上入れる

関係について守ること:
- from と to には、concepts に挙げた概念の label をそのまま書く（id ではなく label）。同じ概念を両端にしない
- 資料に明示的に書かれている関係だけを挙げる。一般知識から補わない
- label はラベルの定義に従って選ぶ。無理に当てはめず、合わなければ「{other}」にする
- from と to はラベルの定義が決める向きに従う。逆向きの関係ならば from と to を入れ替える
- from と to の概念の種類が、ラベルの【向き】に書かれた種類でなければ、そのラベルは使わない
- A が B の定義そのもの、または A と B が同じものの言い換えである場合、それに合うラベルが無ければ「{other}」にする。ほかのラベルに押し込まない
{structure_rules}
- page はその関係が書かれているページの番号（p.{first}〜p.{last} から）
- evidence には、その関係が書かれている箇所を page のページ本文からそのまま 40 字以内で抜き出す。抜き出せない関係は出さない
- evidence は本文の連続した文字をそのまま写す。括弧書き（原語・訳語・読み）、かぎ括弧、記号も省かない。
  離れた箇所をつながない。言い換えない
- 同じページに二つの概念が出てくるだけでは関係にしない。二つの概念を結びつける記述があるときだけ挙げる
- concepts に挙げた概念は、資料にほかの概念と結びつける記述があれば、できるだけ関係にも入れる。
  最後に、どの関係にも入っていない概念を見直し、資料に結びつける記述があればその関係を足す
- 見出しや「次のようなものがある」などの下に項目が並んでいるときは、項目のそれぞれを、まとめる側の概念と結ぶ
  （例・種類を表すラベルがあればそれを使い、無ければ「{other}」）
- 1 度しか書かれていない関係も挙げる。数の上限はない

=== 資料 ===
{text}
"""
EXTRACT_USER = EXTRACT_USER.replace("{structure_rules}", STRUCTURE_RULES)

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


def _name_head(label: str) -> str:
    """name_in で探す名前: 括弧の前。空白は残す（英語の名前を語の区切りで探すため、#135）。"""
    return normalize_text(label).split("(")[0].strip()


def _label_head(label: str) -> str:
    """概念名の照合用。括弧より前（例: 「理（ロゴス）を伴う活動」→「理」）。"""
    return _squash(label).split("(")[0]


# 箇条書きの記号。PDF では u になることがあるが、英文の単語と取り違えないよう、行頭か全角文字の直後の u だけにする
_BULLET_U_RE = re.compile(r"(^|[\u3000-\uffff])(\s*)u(?=\s|[\u3000-\uffff])", re.M)
_BULLET_RE = re.compile(r"[•▪■□●○◆◇◦‣➢➤✓](?=\s)")
_DATE_RE = re.compile(r"\d{4}/\d{1,2}/\d{1,2}")                          # スライドの見出しの日付
_GLOSS_RE = re.compile(r"\([^()]*\)")                                    # 括弧書き（NFKC の後なので半角）
_QUOTE_RE = re.compile(r"[「」『』\"“”]")
_DASH_RE = re.compile(r"[\u2010-\u2015\u2212\ufe58\ufe63]")               # ハイフン・ダッシュ・マイナス（#119）
_APOS_RE = re.compile(r"['‘’`′]")                                       # アポストロフィ（#119。Euler’s と Eulers）


def _loose(text: str) -> str:
    """evidence の照合の第 2 段（#114）: 箇条書きの記号・日付・括弧書き・かぎ括弧を除いて空白を詰める。
    マイナスと各種のダッシュは「-」にそろえ、アポストロフィは除く（#119）。"""
    t = _BULLET_U_RE.sub(r"\1\2 ", _BULLET_RE.sub(" ", normalize_text(text)))
    t = _DATE_RE.sub("", t)
    t = _SPACE_RE.sub("", t)
    while True:
        u = _GLOSS_RE.sub("", t)
        if u == t:
            break
        t = u
    return _APOS_RE.sub("", _DASH_RE.sub("-", _QUOTE_RE.sub("", t)))


def evidence_in(evidence: str, text: str) -> str | None:
    """evidence が text の中にあるか。"exact"（空白だけを詰めて一致）、"loose"（_loose で一致）、None。

    loose は、LLM が本文の括弧書き（原語・訳語）やかぎ括弧、箇条書きの記号、見出しの日付を抜いて写したものを通す。
    本文に無い文字を足したもの・離れた箇所をつないだものは通さない。
    """
    ev = _squash(evidence)
    if not ev:
        return None
    if ev in _squash(text):
        return "exact"
    lev = _loose(evidence)
    return "loose" if len(lev) >= 4 and lev in _loose(text) else None


_NAME_DROP_RE = re.compile(r"[\u2010-\u2015\u2212\-'‘’`′]")


def _name_fold(text: str) -> str:
    """概念名を本文・evidence の中で探すための形（#127）: 空白を除き、ラテン文字は小文字にし、ハイフン・ダッシュ・
    アポストロフィを除く（Normal Distribution と normal distribution、Euler’s と Eulers を同じに見る）。"""
    return _NAME_DROP_RE.sub("", _ASCII_UPPER_RE.sub(lambda m: m.group().lower(), _squash(text)))


def _name_keys(name: str) -> list[str]:
    """_name_fold した名前と、英語の名前なら最後の s を除いた形（Vectors → vector。ss・us・is・ics は除かない）。"""
    k = _name_fold(name)
    out = [k] if k else []
    if _LATIN_RE.search(name) and not _CJK_RE.search(name) and len(k) >= 4 and k.endswith("s") \
            and not _KEEP_FINAL_S_RE.search(k):
        out.append(k[:-1])
    return out


_WORD_SEP_RE = re.compile(r"[\s\u2010-\u2015\u2212_-]+")


def _latin_name_re(name: str) -> re.Pattern | None:
    """英語などの名前を、語の区切りで探す正規表現（#135）。大文字・小文字、語の間の空白・ハイフン、
    最後の語の複数形の s・es を区別しない。日本語などを含む名前は None。"""
    s = _APOS_RE.sub("", normalize_text(name))
    if not _LATIN_RE.search(s) or _CJK_RE.search(s):
        return None
    words = [w for w in _WORD_SEP_RE.split(s) if w]
    if not words:
        return None
    last = words[-1]
    if len(last) >= 4 and last.lower().endswith("s") and not _KEEP_FINAL_S_RE.search(last.lower()):
        last = last[:-1]
    body = r"[\s\-]*".join(re.escape(w) for w in words[:-1] + [last])
    return re.compile(r"(?<![A-Za-z0-9])" + body + r"(?:e?s)?(?![A-Za-z0-9])", re.I)


def _latin_text(text: str) -> str:
    return _WORD_SEP_RE.sub(" ", _APOS_RE.sub("", _DASH_RE.sub("-", normalize_text(text))))


def name_in(name: str, text: str, longer: tuple[str, ...] = ()) -> bool:
    """概念名 name が text にあるか。英語などの名前は語の区切りで探し（mean は meant に当たらない）、
    日本語などは _name_keys のどれかが _name_fold(text) に含まれるか。
    longer（name を含む、より長いほかの概念名）の中に出るだけの name は数えない（#135。
    「exponential function」の中の「Exponential」）。"""
    pat = _latin_name_re(name)
    if pat is not None:
        t = _latin_text(text)
        for other in longer:
            op = _latin_name_re(other)
            if op is not None:
                t = op.sub(" ", t)
        return bool(pat.search(t))
    t = _name_fold(text)
    for other in longer:
        for k in _name_keys(other):
            t = t.replace(k, "\0")
    return any(k in t for k in _name_keys(name))


def longer_names(name: str, names) -> tuple[str, ...]:
    """names のうち、name を含むより長い、別の概念の名前（name_in の longer に渡す）。
    統合の鍵（merge_key）が同じ名前（複数形・大文字小文字・括弧書きの違い）は別の概念ではないので除く。"""
    keys, own = _name_keys(name), merge_key(name)
    return tuple(n for n in dict.fromkeys(names)
                 if merge_key(n) != own and len(_name_fold(n)) > len(_name_fold(name))
                 and any(k in _name_fold(n) for k in keys))


def check_evidence(evidence: str, end_labels: list[str], page_text: str) -> str | None:
    """関係が出典ページに根拠を持つか（Issue #11）。落とす理由、または None を返す。"""
    if not evidence_in(evidence, page_text):
        return "evidence が本文に無い"
    heads = [_name_head(l) for l in end_labels]
    if not any(h and name_in(h, page_text) for h in heads):
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
        parts.append(f"[第{lec.number}回 p.{i}] {lec.prompt_page(i)}")
    return "\n".join(parts)


def _direction_text(rel: dict, wide: list[str] | None = None) -> str:
    """関係ラベルの向き（from 側・to 側の種類）。旧形式の vocab.json には無いので空にする。
    wide（concept_label_kinds）があれば、両端にその種類のどれでもよいことを添える（#119）。"""
    f, t = rel.get("from_kinds") or [], rel.get("to_kinds") or []
    if not f and not t:
        return ""
    extra = f"。両端とも {'・'.join(wide)} のどれでもよい（向きは定義に従う）" if wide else ""
    return f"【向き: from は {'・'.join(f) or '指定なし'}、to は {'・'.join(t) or '指定なし'}{extra}】"


def _label_line(rel: dict, wide: dict) -> str:
    """プロンプトに載せるラベルの行。定義・文の型（#155）・向き・例。"""
    sentence = relation_sentence(rel, "{from}", "{to}")
    form = f"【文の型: 「{sentence}」】" if sentence else ""
    return f"- {rel['label']}: {rel['definition']}{form}{_direction_text(rel, wide.get(rel['label']))}（例: {rel['example']}）"


_FORMULA_RE = re.compile(r"[=<>≤≥→]")


def prompt_kind_examples(kind: dict) -> list[str]:
    """プロンプトに載せる種類の実例（#119）。数式・等式の実例は載せない（概念として数式を挙げさせないため）。"""
    return [e for e in kind_examples(kind) if not _FORMULA_RE.search(e)]


# 端の種類が合わない関係の扱い（#49）。"other" はラベルを「その他」にして残す、"drop" は関係を落とす。
# 2 つを比べるために切り替えられるようにしている。run_extraction の引数で渡すか、環境変数で既定を変える
END_KIND_MISMATCH_POLICIES = ("other", "drop")
END_KIND_MISMATCH_DEFAULT = os.environ.get("QUIZMAP_END_KIND_MISMATCH", "other")


def check_end_kinds(f: str, t: str, label: str, concepts: dict[str, dict],
                    rel_by_label: dict[str, dict],
                    wide: dict[str, list[str]] | None = None,
                    allow_swap: bool = True) -> tuple[str, str, str, str | None]:
    """関係の両端の概念の種類が、ラベルの from_kinds・to_kinds に合うか（Issue #49）。LLM は呼ばない。

    合えばそのまま。合わなければ逆向きで合うかを見て、合えば from と to を入れ替える（"swapped"）。
    allow_swap が偽なら入れ替えない（つなぐ工程の 1 段目、#135。入れ替えると意味が逆になる例が多かった）。
    それでも合わず、ラベルが概念系の種類どうしのラベル（wide、concept_label_kinds）で両端がその種類なら、
    向きはそのままで残す（"widened"、#119）。
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
    if allow_swap and kt in fk and kf in tk:
        return t, f, label, "swapped"
    allowed = (wide or {}).get(label)
    if allowed and kf in allowed and kt in allowed:
        return f, t, label, "widened"
    return f, t, OTHER_LABEL, "other"


DROPPED_RELATIONS_MAX = 1000   # stats に残す、落とした関係の数の上限
# かたまりは文字数で作る（#107。スライドは 1 ページの文字が少ないことが多い）。0 以下ならページ数（CONCEPT_PAGES）で作る
CHUNK_CHARS = int(os.environ.get("QUIZMAP_CHUNK_CHARS", "4000"))       # 1 回に渡す文字数の目安（空白を除く）
CHUNK_MAX_PAGES = int(os.environ.get("QUIZMAP_CHUNK_MAX_PAGES", "8"))  # 1 回に渡すページ数の上限（重ねるページを除く）
CHUNK_OVERLAP = int(os.environ.get("QUIZMAP_CHUNK_OVERLAP", "1"))      # 前のかたまりと重ねるページ数
EXTRACT_WORKERS = int(os.environ.get("QUIZMAP_EXTRACT_WORKERS", "4"))  # かたまりを並列に呼ぶ本数
_MERGE_DROP = _SPACE_ONLY | set("・･=＝")


def page_chars(text: str) -> int:
    """かたまりの大きさを測る、ページの文字数（空白を除く）。"""
    return len(_squash(text))


def char_chunks(page_texts: list[str], chars: int = CHUNK_CHARS,
                max_pages: int = CHUNK_MAX_PAGES) -> list[tuple[int, int]]:
    """ページを文字数でまとめる（#107）。目安の chars 字に達するか max_pages ページになるまで、順にページを足す。

    1 ページで chars を超えるページは 1 つのかたまりにする。最後のかたまりが目安の半分に満たず、前と合わせても
    max_pages を超えなければ、前のかたまりに合わせる。戻り値は (最初のページ, 最後のページ) の列（1 始まり）。
    """
    n = len(page_texts)
    if n == 0:
        return []
    max_pages = max_pages if max_pages > 0 else n
    sizes = [page_chars(t) for t in page_texts]
    chunks: list[list[int]] = []          # [最初, 最後, 文字数]
    for p in range(1, n + 1):
        if not chunks or chunks[-1][2] >= chars or chunks[-1][1] - chunks[-1][0] + 1 >= max_pages:
            chunks.append([p, p, 0])
        chunks[-1][1] = p
        chunks[-1][2] += sizes[p - 1]
    if len(chunks) > 1:
        a, b = chunks[-2], chunks[-1]
        if b[2] < chars / 2 and b[1] - a[0] + 1 <= max_pages:
            chunks[-2:] = [[a[0], b[1], a[2] + b[2]]]
    return [(a, b) for a, b, _ in chunks]


def extraction_chunks(n_pages: int, size: int = CONCEPT_PAGES, overlap: int = CHUNK_OVERLAP,
                      page_texts: list[str] | None = None, chars: int = 0,
                      max_pages: int = CHUNK_MAX_PAGES) -> list[tuple[int, int]]:
    """抽出のページの分け方（#101・#107）。chars が正で page_texts があれば char_chunks（文字数）で、
    そうでなければ page_chunks（size ページ前後ずつ）で分ける。各かたまりの前に、前のかたまりの最後の overlap ページを
    重ねる（境目をまたいで書かれた関係を失わないため。重なりで同じ概念・関係が 2 度出ても、統合と同じ対の整理でまとまる）。"""
    base = char_chunks(page_texts, chars, max_pages) if chars > 0 and page_texts is not None \
        else page_chunks(n_pages, size)
    return [(max(1, a - overlap) if i else a, b) for i, (a, b) in enumerate(base)]


_MERGE_QUOTES = set("「」\"“”")
_TRAILING_GLOSS_RE = re.compile(r"^(.+?)\([^()]*\)$")
_ASCII_UPPER_RE = re.compile(r"[A-Z]")
_ID_ONLY_RE = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)+")               # 応答の id の形（小文字と _ だけ、_ を含む）
_LATIN_RE = re.compile(r"[A-Za-z]")
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff々]")
_LATIN_SEP_RE = re.compile(r"[\u2010-\u2015\u2212_-]")               # ハイフン・ダッシュ・下線（語の区切りとして扱う）
_ARTICLES = {"the", "a", "an"}
_KEEP_FINAL_S_RE = re.compile(r"(ss|us|is|ics)$")                     # 複数形の s ではない語尾（class, Bayes 以外の us/is/ics）


def _latin_fold(label: str) -> str:
    """ラテン文字の名前の、統合の鍵の前処理（#119）。日本語などを含む名前はそのまま。
    ハイフン・ダッシュ・下線を空白にし、アポストロフィを除き、先頭の冠詞を除き、最後の語の複数形の s を除く
    （points → point、Eigenvalues → Eigenvalue。statistics・analysis・class は除かない）。"""
    s = normalize_text(label)
    if not _LATIN_RE.search(s) or _CJK_RE.search(s):
        return label
    s = _APOS_RE.sub("", _LATIN_SEP_RE.sub(" ", s))
    words = s.split()
    if len(words) > 1 and words[0].lower() in _ARTICLES:
        words = words[1:]
    if words and len(words[-1]) >= 4 and words[-1].endswith("s") and not _KEEP_FINAL_S_RE.search(words[-1].lower()):
        words[-1] = words[-1][:-1]
    return " ".join(words)


def merge_key(label: str) -> str:
    """概念を同じものとしてまとめる鍵（#107・#114）: NFKC にし、空白・中黒・イコールの違いを無視する。
    かぎ括弧（「」）の有無と、末尾の括弧書き（「幸福(エウダイモニアー)」の訳語・原語）の有無も無視する。
    『』は無視しない（書名と、同じ名前の学説を分けるため）。括弧の中の語（ロゴス）とは別の概念のまま。
    ラテン文字の大文字・小文字は区別しない（#119。Derivative / derivative）。ただし 3 字未満の鍵（変数名など。P と p）は区別する。
    英語などの名前は、単数・複数、先頭の冠詞、ハイフンと空白、アポストロフィの違いも無視する（_latin_fold、#119）。"""
    key = _folded(_latin_fold(label), _MERGE_DROP | _MERGE_QUOTES)[0]
    m = _TRAILING_GLOSS_RE.match(key)
    key = m.group(1) if m else key
    return _ASCII_UPPER_RE.sub(lambda c: c.group().lower(), key) if len(key) >= 3 else key


_TITLE_RE = re.compile(r"『[^『』]+』")
_PERSON_SPLIT_RE = re.compile(r"[・･=＝\s]+")
_LATIN_NAME_SPLIT_RE = re.compile(r"[\s.]+")
_TITLES = {"dr", "prof", "mr", "mrs", "ms", "sir"}


def _person_name(label: str) -> tuple[str, list[str], bool] | None:
    """人名を (姓, 名の並び, ラテン文字か) に分ける（#127 のつづき）。括弧書きは除く。分けられなければ None。
    日本語などは「・」「=」・空白で区切った最後を姓に、ラテン文字は空白・「.」で区切った最後を姓にする
    （L.M.Sullivan → Sullivan、名は L・M）。"""
    head = normalize_text(label).split("(")[0].strip()
    latin = bool(_LATIN_RE.search(head)) and not _CJK_RE.search(head)
    parts = [p for p in (_LATIN_NAME_SPLIT_RE if latin else _PERSON_SPLIT_RE).split(_APOS_RE.sub("", head)) if p]
    if not parts:
        return None
    surname = parts[-1].lower() if latin else _squash(parts[-1])
    if len(surname) < 2:
        return None
    given = [p.lower() if latin else _squash(p).rstrip(".") for p in parts[:-1]]
    given = [g for g in given if g]
    if latin:
        given = [g for g in given if g not in _TITLES]
    return surname, given, latin


# 片仮名の名の最初の字から考えられるラテン文字の頭文字（G. W. ヘーゲル と ゲオルク・ヴィルヘルム・ヘーゲル を比べるため）
_KANA_INITIALS = {}
for _kana, _letters in {
    "アァ": "A", "イィ": "IEY", "ウゥ": "UWO", "エェ": "EA", "オォ": "OA",
    "カキクケコ": "KC", "ガギグゲゴ": "G", "サスセソ": "SC", "シ": "S", "ザズゼゾ": "Z", "ジ": "JGZ",
    "タテト": "T", "チ": "CT", "ツ": "TZ", "ダデド": "D", "ヂヅ": "DZ", "ナニヌネノ": "N",
    "ハヒヘホ": "H", "フ": "FH", "バビブベボ": "BV", "パピプペポ": "P", "マミムメモ": "M",
    "ヤユヨャュョ": "YJ", "ラリルレロ": "RL", "ワ": "WV", "ヲ": "O", "ヴ": "VW",
}.items():
    for _k in _kana:
        _KANA_INITIALS[_k] = set(_letters)
_INITIAL_RE = re.compile(r"[A-Za-z]{1,2}")


def _given_compatible(x: str, y: str) -> bool:
    """名の 1 語どうしが食い違わないか（日本語などの表記の人名）。同じ語、またはラテン文字の頭文字と片仮名の名で
    片仮名の最初の字の音が頭文字に合うもの（G と ゲオルク、W と ヴィルヘルム）。"""
    if x == y:
        return True
    xi, yi = bool(_INITIAL_RE.fullmatch(x)), bool(_INITIAL_RE.fullmatch(y))
    if xi and yi:
        return x[0].upper() == y[0].upper()
    if xi != yi:
        initial, kana = (x, y) if xi else (y, x)
        return initial[0].upper() in _KANA_INITIALS.get(kana[:1], set())
    return False


def _same_person(a: tuple, b: tuple) -> bool:
    """_person_name の 2 つが同じ人か。姓が同じで、名の短い側が長い側と食い違わないこと。
    ラテン文字は名を頭文字で比べ（Lisa と L）、先頭からそろえて比べる。日本語などは、名の無い側（姓だけ）とだけ合わせる。
    名がどちらにもあって違う（頭文字が違う）ときは別人とみなす。"""
    if a[0] != b[0] or a[2] != b[2]:
        return False
    ga, gb = a[1], b[1]
    if not ga or not gb:
        return True                                  # 片方が姓だけ
    if not a[2]:
        return all(_given_compatible(x, y) for x, y in zip(ga, gb))
    return all(x[0] == y[0] and (len(x) == 1 or len(y) == 1 or x == y) for x, y in zip(ga, gb))


def person_aliases(concepts: dict[str, dict], person_kinds: set[str],
                   weight: dict[str, int] | None = None) -> dict[str, str]:
    """人物の種類の概念で、同じ人の別の書き方（姓だけ・頭文字）をまとめる先（#127 のつづき）。{まとめる id: 残す id}。
    LLM は呼ばない。ある概念が別人どうしの 2 人以上と合ってしまうとき（姓だけで、同じ姓の別人がいる）はまとめない。
    残すのは weight（挙げられた回数）の多いほう、同じなら先に出たほう。"""
    weight = weight or {}
    people = [(cid, n) for cid, c in concepts.items() if c.get("kind") in person_kinds
              if (n := _person_name(c["label"])) is not None]
    order = {cid: i for i, cid in enumerate(concepts)}
    matches = {cid: [o for o, m in people if o != cid and _same_person(n, m)] for cid, n in people}
    parent = {cid: cid for cid, _ in people}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    named = dict(people)
    # 1) 名が両方に書かれた組どうしを先にまとめる（G. W. ヘーゲル と ゲオルク・ヴィルヘルム・フリードリヒ・ヘーゲル）。
    #    ある名前が、互いに別人の 2 人と合うときは、その名前はまとめない
    full = {cid for cid, n in people if n[1]}

    def ambiguous(cid, others):
        return any(not _same_person(named[x], named[y]) for i, x in enumerate(others) for y in others[i + 1:])

    for cid in full:
        others = [o for o in matches[cid] if o in full]
        if not ambiguous(cid, others):
            for o in others:
                parent[find(cid)] = find(o)
    # 1b) 区切りの無い漢字の名前（宮坂 と 宮坂道夫）: 短い名前（2 字以上）が長い名前の先頭と同じで、
    #     合う長い名前が 1 人だけのときに合わせる（日本語の人名は姓が先で、区切りが無い）
    kanji_names = {cid: n[0] for cid, n in people if not n[2] and not n[1] and _KANJI_RE.search(n[0])}
    for cid, short in kanji_names.items():
        if len(short) < 2:
            continue
        longer = [o for o, name in kanji_names.items() if o != cid and len(name) > len(short) and name.startswith(short)]
        if len(longer) == 1 and not any(o != cid and name.startswith(short) and len(name) == len(short)
                                        for o, name in kanji_names.items()):
            parent[find(cid)] = find(longer[0])
    # 2) 姓だけの名前は、合う相手がみな 1 つの組（同じ人）にまとまっているときだけ、その組に合わせる
    for cid, n in people:
        if n[1] or not matches[cid]:
            continue
        roots = {find(o) for o in matches[cid] if o in full} or {find(o) for o in matches[cid]}
        if len(roots) == 1:
            parent[find(cid)] = roots.pop()
    groups: dict[str, list[str]] = collections.defaultdict(list)
    for cid, _ in people:
        groups[find(cid)].append(cid)
    out = {}
    for ids in groups.values():
        if len(ids) < 2:
            continue
        keep = min(ids, key=lambda x: (-weight.get(x, 0), order[x]))
        out.update({x: keep for x in ids if x != keep})
    return out


def _majority_kind(kinds: list[str]) -> str:
    """まとめた概念の種類（#119）: 多く挙がった種類。「その他」より vocab の種類を先にし、同じなら先に出たほう。"""
    counts = collections.Counter(kinds)
    return min(dict.fromkeys(kinds), key=lambda k: (k == OTHER_LABEL, -counts[k], kinds.index(k)))


def _display_label(counts: collections.Counter, first: list[str]) -> str:
    """まとめた概念の地図に出す表記（#114）: 挙げられた回数の多い表記。同じなら短いほう、それも同じなら先に出たほう。"""
    return min(first, key=lambda l: (-counts[l], len(l), first.index(l)))


def extract_graph(client, model: str, course: str, vocab: dict, lectures: list[Lecture], log=print,
                  step=lambda name: None, pages_per_call: int = CONCEPT_PAGES, overlap: int = CHUNK_OVERLAP,
                  chunk_chars: int = CHUNK_CHARS, max_pages: int = CHUNK_MAX_PAGES,
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
        + (f"（例: {'、'.join(ex)}）" if (ex := prompt_kind_examples(k)) else "") for k in vocab["kinds"]
    )
    wide = concept_label_kinds(vocab)
    rel_text = "\n".join(
        _label_line(r, wide)
        for r in vocab["relations"] if r["label"] != OTHER_LABEL
    )
    kind_names = {k["name"] for k in vocab["kinds"]}
    person_kinds = {k["name"] for k in vocab["kinds"] if k.get("person") is True}
    labels = {r["label"] for r in vocab["relations"]} | {OTHER_LABEL}
    rel_by_label = {r["label"]: r for r in vocab["relations"]}

    jobs = [(lec, first, last) for lec in lectures
            for first, last in extraction_chunks(lec.page_count, pages_per_call, overlap,
                                                 page_texts=lec.pages, chars=chunk_chars, max_pages=max_pages)]
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
            "chunk_chars": chunk_chars, "max_pages": max_pages,
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
                if r.get("ask_pages"):
                    m["ask_pages"] = r["ask_pages"]

    # 3b) 直した表記（聞き直しを含む）が元の名前より短く、別の名前で挙がった概念と同じ表記になったものは、
    #     まとめるとその概念を失う（「徳倫理学」→「徳」）。元の名前が本文にあるページを同じ回の中で探して
    #     出典を付け替える。それでも無ければ地図に載せず concepts_without_surface に回す（#101）
    strong_keys = {merge_key(m["surface"]) for m in mentions if m["surface"] and m["how"] == "そのまま"}
    relocated = collided = 0

    def shortened_into_other(label: str, surface: str) -> bool:
        k = merge_key(surface)
        return len(_squash(surface)) < len(_squash(label)) and k in strong_keys and k != merge_key(label)

    for m in mentions:
        if not m["surface"] or m["how"] == "そのまま" or not shortened_into_other(m["label"], m["surface"]):
            continue
        lec = jobs[m["job"]][0]
        lecture_texts = {p: lec.pages[p - 1] for p in range(1, lec.page_count + 1)}
        found = find_surface(m["label"], m["kind"], lecture_texts, person_kinds, m["claimed"])
        m["collided_with"] = m["surface"]
        if found and not shortened_into_other(m["label"], found[0]):
            m["surface"], m["how"], m["page"] = found[0], "同じ回の別のページ", found[2]
            m["ask_pages"] = pages_with_text(found[0], lecture_texts) or [found[2]]
            relocated += 1
        else:
            m["surface"] = m["how"] = m["page"] = None
            collided += 1
    info["surface_collisions_relocated"] = relocated
    info["surface_collisions_without"] = collided
    if relocated or collided:
        log(f"直した表記が別の概念と重なった {relocated + collided} 件: 同じ回の別のページに付け替えた {relocated}、"
            f"地図に載せず記録した {collided}")

    # 4) 本文の表記でまとめ、id を振る（かたまりの順）
    concepts: dict[str, dict] = {}
    by_key: dict[str, str] = {}
    fixes: list[dict] = []
    without_surface: list[dict] = []
    local: list[dict[str, str]] = [{} for _ in jobs]     # かたまりごと: 応答の label → 概念 id
    local_ids: list[dict[str, str]] = [{} for _ in jobs]  # かたまりごと: 応答の id → 概念 id（#119）
    by_answer_id: dict[str, str] = {}                     # 応答の id → 概念 id（最初に出たもの）
    first_label_of: dict[str, set[str]] = collections.defaultdict(set)
    surface_count: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    surface_order: dict[str, list[str]] = collections.defaultdict(list)
    kind_order: dict[str, list[str]] = collections.defaultdict(list)    # まとめた挙げ方の種類（出た順、重複あり）
    new_of = collections.Counter()
    for m in mentions:
        lec, first, last = jobs[m["job"]]
        if not m["surface"]:
            if not m.get("dup_of_pending") and all(w["label"] != m["label"] or w["lecture"] != lec.number
                                                    for w in without_surface):
                without_surface.append({"label": m["label"], "kind": m["kind"], "definition": m["definition"],
                                        "lecture": lec.number, "pages": m["claimed"],
                                        "asked_surface": m.get("asked_surface", ""),
                                        **({"collided_with": m["collided_with"]} if m.get("collided_with") else {})})
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
        surface_count[cid][m["surface"]] += 1
        kind_order[cid].append(m["kind"])
        if m["surface"] not in surface_order[cid]:
            surface_order[cid].append(m["surface"])
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
        if aid := clean_id(m.get("id") or "", ""):
            local_ids[m["job"]].setdefault(aid, cid)
            by_answer_id.setdefault(aid, cid)
    for cid, c in concepts.items():
        c["sources"].sort(key=lambda s: (s["lecture"], s["page"]))
        c["lectures"].sort()
        c["label"] = _display_label(surface_count[cid], surface_order[cid])
        c["kind"] = _majority_kind(kind_order[cid])
    # 『』で囲まれた名前（作品の題名）は人物ではない（#127 のつづき。作品が人物の種類になっていた）。
    # 地図からは消さず、種類を「その他」にする（本の種類にすると出典として落ちるので）
    titled = [cid for cid, c in concepts.items() if c["kind"] in person_kinds and _TITLE_RE.fullmatch(c["label"].strip())]
    for cid in titled:
        concepts[cid]["kind"] = OTHER_LABEL
    info["titles_not_person"] = [concepts[cid]["label"] for cid in titled]
    # 同じ人物の別の書き方（姓だけ・頭文字。カント / イマヌエル・カント、L.M.Sullivan / Lisa M. Sullivan）をまとめる
    alias = person_aliases(concepts, person_kinds, {cid: sum(v.values()) for cid, v in surface_count.items()})
    merged_people = collections.defaultdict(list)
    for b, a in alias.items():
        ca, cb = concepts[a], concepts.pop(b)
        merged_people[a].append(cb["label"])
        ca["sources"] = sorted({(s["lecture"], s["page"]): s for s in ca["sources"] + cb["sources"]}.values(),
                               key=lambda s: (s["lecture"], s["page"]))
        ca["lectures"] = sorted(set(ca["lectures"]) | set(cb["lectures"]))
        surface_count[a].update(surface_count.pop(b, {}))
        surface_order[a] += [l for l in surface_order.pop(b, []) if l not in surface_order[a]]
        first_label_of[a] |= first_label_of.pop(b, set())
    for a in merged_people:
        merged_people[a].append(concepts[a]["label"])
        concepts[a]["label"] = _display_label(surface_count[a], surface_order[a])
    if alias:
        for table in [by_key, by_answer_id, *local, *local_ids]:
            for k, v in table.items():
                table[k] = alias.get(v, v)
        log(f"同じ人物の別の書き方をまとめた {len(alias)} 件: "
            + " / ".join("・".join(sorted(set(v))) + f" → {concepts[a]['label']}" for a, v in merged_people.items()))
    info["merged_people"] = [sorted(set(v)) for v in merged_people.values()]
    n_mentions = sum(1 for m in mentions if m["surface"])
    info.update({
        "mentions": n_mentions,
        "merged_mentions": n_mentions - len(concepts),                       # 統合でまとめた挙げ方の数
        "merged_variants": sum(len(v) - 1 for v in first_label_of.values()),  # 表記の違い（空白・中黒・括弧書きなど）をまとめた数
        "merged_variant_examples": [sorted(v) for v in first_label_of.values() if len(v) > 1][:SURFACE_EXAMPLES],
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
    dropped_list: list[dict] = []          # 落とした関係（#114。evidence は本文なので残さない）
    loose_evidence = 0

    def drop(reason: str, r: dict, lec_no: int):
        dropped[reason] += 1
        if len(dropped_list) < DROPPED_RELATIONS_MAX:
            dropped_list.append({"reason": reason, "from": r["from"], "to": r["to"], "label": r["label"],
                                 "lecture": lec_no, "page": r["page"]})

    def end_id(name: str, i: int, chunk_texts: dict[int, str]) -> str | None:
        name = name.strip()
        loc = local[i]
        if name in loc:
            return loc[name]
        key = merge_key(name)
        for lab, cid in loc.items():
            if merge_key(lab) == key:
                return cid
        # 端に概念の id（snake_case）を書いてくることがある（#119）。そのかたまりの応答の id、次にほかのかたまりの id
        aid = clean_id(name, "")
        if aid in local_ids[i]:
            return local_ids[i][aid]
        cid = by_key.get(key)                 # ほかのかたまりで挙がった概念で、名前（どれかの表記）がこのかたまりの本文にあるもの
        if cid is None:
            cid = by_answer_id.get(aid) if _ID_ONLY_RE.fullmatch(name) else None
        if cid and any(pages_with_text(l, chunk_texts) for l in surface_order[cid]):
            return cid
        return None

    for i, ((lec, first, last), data) in enumerate(zip(jobs, results)):
        chunk_texts = {p: lec.pages[p - 1] for p in range(first, last + 1)}
        for r in data["relations"]:
            f, t = end_id(r["from"], i, chunk_texts), end_id(r["to"], i, chunk_texts)
            label = r["label"].strip()
            if f is None or t is None:
                names = [r["from"], r["to"]]
                drop("端が資料の表記に無い" if any(merge_key(n) in unresolved for n in names)
                     else "存在しない概念", r, lec.number)
                continue
            if f == t:
                drop("自己ループ", r, lec.number)
                continue
            if label not in labels:
                dropped["vocab にないラベル→その他"] += 1
                label = OTHER_LABEL
            f, t, label, fix = check_end_kinds(f, t, label, concepts, rel_by_label, wide)
            if fix == "swapped":
                adjusted["向きを直した"] += 1
            elif fix == "widened":
                adjusted["端の種類を広げて残した"] += 1
            elif fix == "other" and end_kind_mismatch == "drop":
                drop("端の種類が合わない", r, lec.number)
                continue
            elif fix == "other":
                dropped["端の種類が合わない→その他"] += 1
            if not first <= r["page"] <= last:
                drop("ページ番号が範囲外", r, lec.number)
                continue
            page = r["page"]
            ends = [concepts[f]["label"], concepts[t]["label"]]
            r["evidence"] = r.get("evidence") or ""
            if not _squash(r["evidence"]):
                drop("evidence が空", r, lec.number)
                continue
            reason = check_evidence(r["evidence"], ends, lec.pages[page - 1])
            if reason:
                drop(reason, r, lec.number)
                continue
            if evidence_in(r.get("evidence", ""), lec.pages[page - 1]) == "loose":
                loose_evidence += 1
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
    info["dropped_relations"] = dropped_list
    info["loose_evidence"] = loose_evidence
    size_text = f"{chunk_chars} 字前後・{max_pages} ページまで" if chunk_chars > 0 else f"{pages_per_call} ページ前後"
    log(f"抽出の呼び出し {info['calls']} 回（かたまり {len(jobs)} 個、{size_text}ずつ・{overlap} ページ重ねる・"
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


KIND_DOMINANCE_SHARE = 0.6    # 1 つの種類が概念のこれより多くを占めたら、語彙の種類が粗いとみなす（#150）


def kind_dominance(kind_counts: collections.Counter, share: float = KIND_DOMINANCE_SHARE) -> dict:
    """いちばん多い種類と、その割合（#150）。share を超えたら warning を真にする（語彙の作り直しの目安。作り直しはしない）。
    「その他」の種類は数えない。"""
    counts = {k: n for k, n in kind_counts.items() if k != OTHER_LABEL}
    total = sum(counts.values())
    if not total:
        return {"kind": "", "share": 0.0, "warning": False}
    kind, n = max(counts.items(), key=lambda kv: kv[1])
    return {"kind": kind, "share": round(n / total, 4), "warning": n / total > share}


_TITLE_IN_RE = re.compile(r"『([^『』]+)』")


def same_title_ids(concepts: dict[str, dict], removed_labels: list[str]) -> list[str]:
    """落とした本の題名（『』の中）と同じ題名を含む概念（著者名付きの「ミヒャエル・エンデ『モモ』」など、#157）。"""
    titles = {_squash(t) for label in removed_labels for t in _TITLE_IN_RE.findall(label)}
    return [cid for cid, c in concepts.items()
            if any(_squash(t) in titles for t in _TITLE_IN_RE.findall(c["label"]))]


TITLE_CHECK_SYSTEM = """あなたは大学の授業資料を読み、資料に出てくる本・作品の扱いを判定する専門家です。"""

TITLE_CHECK_USER = """授業「{course}」の資料に、『』の題名を含む次の名前が出てきます。各名前について、授業がその作品の内容
（筋・登場人物・場面・その作品の中の主張）そのものを論じているなら discussed を true、出典・参考・引用元・紹介として
名前が挙がるだけなら false にしてください。判断は、下の資料のページの記述だけで行います。

{items}

=== 資料のページ ===
{pages}
"""

TITLE_CHECK_SCHEMA = {
    "type": "object",
    "properties": {"answers": {"type": "array", "items": {
        "type": "object",
        "properties": {"index": {"type": "integer"}, "discussed": {"type": "boolean"}, "reason": {"type": "string"}},
        "required": ["index", "discussed", "reason"], "additionalProperties": False}}},
    "required": ["answers"],
    "additionalProperties": False,
}
TITLE_CHECK_PAGES = 2   # 1 つの名前に付けるページの数


def check_titled_works(client, model: str, course: str, concepts: dict[str, dict], lectures: list[Lecture],
                       log=print) -> tuple[dict[str, dict], int]:
    """『』の題名を含む概念が、授業で内容が論じられる作品か、名前が挙がるだけの本かを LLM に確かめる（#157）。
    1 回の呼び出しでまとめて判定する。戻り値: ({概念 id: {"discussed", "reason"}}, 呼び出し回数)。
    答えの無い概念は残す側（discussed: true）にする。"""
    by_lec = {lec.number: lec for lec in lectures}
    ids = list(concepts)
    keys = []
    lines = []
    for i, cid in enumerate(ids, 1):
        c = concepts[cid]
        srcs = [(s["lecture"], s["page"]) for s in c.get("sources", [])][:TITLE_CHECK_PAGES]
        keys += [k for k in srcs if k not in keys]
        lines.append(f"{i}. {c['label']}（{c.get('definition', '')}）ページ: "
                     + "、".join(f"第{l}回 p{p}" for l, p in srcs))
    pages = "\n\n".join(f"[第{l}回 p{p}]\n{by_lec[l].pages[p - 1][:PAGE_CHARS]}" for l, p in keys
                         if l in by_lec and 0 < p <= len(by_lec[l].pages))
    data = call_json(client, model=model, system=TITLE_CHECK_SYSTEM, schema=TITLE_CHECK_SCHEMA,
                     user=TITLE_CHECK_USER.format(course=course, items="\n".join(lines), pages=pages or "（なし）"))
    out = {cid: {"discussed": True, "reason": "答えが無い"} for cid in ids}
    for a in data.get("answers", []):
        if 1 <= a.get("index", 0) <= len(ids):
            out[ids[a["index"] - 1]] = {"discussed": bool(a["discussed"]), "reason": a.get("reason", "")}
    log("『』の題名を含む名前の判定: " + " / ".join(
        f"{concepts[cid]['label']}（{'内容を論じる' if v['discussed'] else '名前が挙がるだけ'}）" for cid, v in out.items()))
    return out, 1


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
{structure_rules}
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
COMPLETION_USER = COMPLETION_USER.replace("{structure_rules}", STRUCTURE_RULES)

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
    """関係を補う候補の対を選ぶ。LLM も埋め込みも呼ばない（client は使わない。呼ぶ側との互換のために残す）。

    1. 同じページに一緒に出てくるのに関係の無い対
    2. どの関係にもつながらない概念は、その概念が出てくるページの索引ベクトルの平均で近いページを探し（#107。
       埋め込みは呼ばない）、そのページに出てくる概念と対にする（索引が無ければ 1 だけ）
    どの関係にもつながらない概念を含む対を先に選ぶ。そのときは、つながらない概念 1 個につき 1 対ずつ順に選び（#114）、
    一巡したら 2 対目へ進む（同じ概念が何対も取って、ほかの概念が判定まで届かないのを防ぐ）。概念ごとの対は、
    一緒に出るページの多い順、検索の近さの順。残りの枠に、つながる概念どうしの対を一緒に出るページの多い順に入れる。
    max_pairs 本で切る。
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
        own = {cid: _pages_of(concepts[cid]) for cid in isolated}
        results = search_by_own_pages(index, [own[cid] for cid in isolated], k=search_k)
        searches = sum(1 for found in results if found is not None)
        for cid, found in zip(isolated, results):
            for h in found or []:
                page = (h["lecture"], h["page"])
                for other in on_page.get(page, []):
                    add(cid, other, page, shared=False, score=h.get("score"))

    chosen = _pick_pairs(pairs.values(), set(isolated), max_pairs)
    for p in chosen:
        p["pages"] = p["pages"][:COMPLETION_PAGES_PER_PAIR]
        p["page_info"] = p["page_info"][:COMPLETION_PAGES_PER_PAIR]
        p.pop("shared")
    return chosen, {"candidates": len(pairs), "judged": len(chosen), "isolated_before": len(isolated),
                    "searches": searches}


def search_by_own_pages(index, page_sets: list[set[tuple[int, int]]], k: int) -> list[list[dict] | None]:
    """ページの組ごとに、その組のページの索引ベクトルの平均に近いページを k 件（#107）。埋め込みは呼ばない。

    index は SlideIndex（pages と、長さ 1 に正規化した vectors を持つ）。組のページと本文が空のページは返さない。
    結果は page_sets と同じ順の [{"lecture", "page", "text", "score"}]（score はコサイン類似度、降順）。
    組のページがどれも索引に無ければ None（検索しなかった）。
    """
    import numpy as np

    pos = {(p["lecture"], p["page"]): i for i, p in enumerate(index.pages)}
    vectors = np.asarray(index.vectors, dtype=np.float32)
    usable = [bool(p["text"].strip()) for p in index.pages]
    out: list[list[dict] | None] = []
    for pages in page_sets:
        rows = [pos[p] for p in sorted(pages) if p in pos]
        if not rows or k <= 0:
            out.append(None if not rows else [])
            continue
        q = vectors[rows].mean(axis=0)
        n = np.linalg.norm(q)
        if not n:
            out.append([])
            continue
        scores = vectors @ (q / n)
        hits = []
        for i in np.argsort(-scores, kind="stable"):
            p = index.pages[i]
            if (p["lecture"], p["page"]) in pages or not usable[i]:
                continue
            hits.append({**p, "score": round(float(scores[i]), 4)})
            if len(hits) >= k:
                break
        out.append(hits)
    return out


def _pick_pairs(pairs, isolated: set[str], max_pairs: int) -> list[dict]:
    """completion_candidates の対の選び方（#114）。つながらない概念ごとに 1 対ずつ順に選ぶ。"""
    def best_score(p):
        return max((x["score"] for x in p["page_info"] if x["score"] is not None), default=0.0)

    ranked = sorted(pairs, key=lambda p: (not p["isolated"], -p["shared"], -best_score(p), p["a"], p["b"]))
    own: dict[str, list[dict]] = collections.defaultdict(list)       # つながらない概念 → その概念を含む対（よい順）
    order: list[str] = []
    for p in ranked:
        for cid in (p["a"], p["b"]):
            if cid in isolated:
                if cid not in own:
                    order.append(cid)
                own[cid].append(p)
    chosen: list[dict] = []
    taken: set[int] = set()
    count = collections.Counter()                                    # つながらない概念ごとの、選んだ対の数
    rnd = 0
    while len(chosen) < max_pairs and any(own[c] for c in order):
        rnd += 1
        for cid in order:
            if len(chosen) >= max_pairs:
                break
            if count[cid] >= rnd:                                    # 相手の側で選ばれた対で、この巡は足りている
                continue
            while own[cid] and id(own[cid][0]) in taken:
                own[cid].pop(0)
            if not own[cid]:
                continue
            p = own[cid].pop(0)
            taken.add(id(p))
            chosen.append(p)
            for x in (p["a"], p["b"]):
                if x in isolated:
                    count[x] += 1
    for p in ranked:
        if len(chosen) >= max_pairs:
            break
        if id(p) not in taken:
            taken.add(id(p))
            chosen.append(p)
    return chosen


def _wiki_text(article: dict) -> str:
    return normalize_text(article.get("extract", ""))[:WIKI_CHARS]


def check_external_evidence(evidence: str, end_labels: list[str], article: dict) -> str | None:
    """Wikipedia で確かめた関係の根拠を照合する。落とす理由、または None を返す。

    evidence が記事の冒頭（プロンプトに載せた範囲）にそのまま有ること、
    両端の概念名がどちらも記事の題名か冒頭に出てくること（片方の記事に自分の名前が出るだけでは足りない）。
    """
    if not evidence_in(evidence, _wiki_text(article)):
        return "evidence が記事に無い"
    where = article.get("title", "") + " " + _wiki_text(article)
    if not all(h and name_in(h, where) for h in (_name_head(l) for l in end_labels)):
        return "両端の概念名が記事に無い"
    return None


_NAME_SPLIT_RE = re.compile(r"[・･=＝\s]+")


def _name_variants(concept: dict, person_kinds: set[str]) -> list[str]:
    """evidence の中で概念を見つけるための名前。括弧の前（_label_head）。人物の種類なら「・」などで区切った各部分も
    （「イマヌエル・カント」は「カント」でもよい。区切りの無い名前は全体だけ）。2 字未満の部分は使わない。"""
    names = [_name_head(concept["label"])]
    if concept.get("kind") in person_kinds:
        head = normalize_text(concept["label"]).split("(")[0]
        names += [_squash(x) for x in _NAME_SPLIT_RE.split(head) if len(_squash(x)) >= 2]
    return [n for n in dict.fromkeys(names) if n]


def check_ends_in_evidence(evidence: str, ends: list[dict], person_kinds: set[str]) -> str | None:
    """補完の関係の evidence に、両端の概念名が両方あるか（#92）。無ければ落とす理由を返す。

    別の概念についての記述（片方の名前しか無い）を根拠にした関係を足さないため。迷うものは落とす側に倒す。
    """
    if all(any(name_in(n, evidence) for n in _name_variants(c, person_kinds)) for c in ends):
        return None
    return "evidence に両端が無い"


_FRAGMENT_SEP_RE = re.compile(r"\s*(?:\.{3,}|…+|・・・|‥)\s*")
FRAGMENTS_MAX = 2


def check_fragment_evidence(evidence: str, ends: list[dict], page_text: str, person_kinds: set[str],
                            all_labels: tuple[str, ...] = ()) -> str | None:
    """つなぐ工程の 1 段目の資料の根拠（#127）。落とす理由、または None を返す。

    evidence は同じページの本文から写した 1〜FRAGMENTS_MAX 個の断片（「...」「…」で区切る）でよい。
    断片はそれぞれ本文にそのまま有ること（evidence_in）。両端の名前は、それぞれ断片のどれかか同じページの本文に有り、
    少なくとも一方は断片の中に有ること（どちらの概念にも触れない断片を根拠にしない）。
    名前は名前全体で探し、all_labels のうちその名前を含むより長い概念名の中に出るだけのものは数えない（#135）。
    """
    frags = [f for f in _FRAGMENT_SEP_RE.split(evidence or "") if _squash(f)]
    if not frags:
        return "evidence が空"
    if len(frags) > FRAGMENTS_MAX:
        return "断片が多すぎる"
    if not all(len(_squash(f)) >= 4 and evidence_in(f, page_text) for f in frags):
        return "evidence が本文に無い"
    names = [_name_variants(c, person_kinds) for c in ends]
    longer = {n: longer_names(n, all_labels) for ns in names for n in ns}
    in_frag = [any(name_in(n, f, longer[n]) for n in ns for f in frags) for ns in names]
    if not all(hit or any(name_in(n, page_text, longer[n]) for n in ns) for hit, ns in zip(in_frag, names)):
        return "両端の概念名が本文に無い"
    if not any(in_frag):
        return "evidence にどちらの端も無い"
    return None


def verify_judgement(j: dict, pages_allowed: set, wiki_ids: list[str], articles: dict, concepts: dict,
                     page_text: dict, labels: set[str], rel_by_label: dict, wide: dict,
                     person_kinds: set[str], fragments: bool = False) -> tuple[dict, list[str], str | None]:
    """補完・つなぐ工程の LLM の判定 1 件を照合する（#78・#88・#127）。LLM は呼ばない。

    ラベルが語彙にあるか、端の種類（check_end_kinds）、資料の根拠（ページが pages_allowed にあり、evidence が本文にあり、
    両端の名前が evidence にある）、通らなければ Wikipedia の根拠（記事が wiki_ids にあり両端の記事のどちらかで、
    evidence が記事にあり、両端の名前が evidence にある）の順に見る。
    fragments が真なら、資料の根拠は check_fragment_evidence で見る（つなぐ工程の 1 段目、#127）。
    そのときは向きを入れ替えず、答えの向きで端の種類が合わなければ落とす（#135）。
    戻り値: (関係（origin を持てば通った。external の source は呼ぶ側が決める）, 根拠が通らなかった理由,
             "語彙にないラベル" / "端の種類が合わない"（ここで落とした）/ "swapped" / "widened" / None)
    """
    f, t, label = j["from"].strip(), j["to"].strip(), j["label"].strip()
    if label not in labels:
        return {}, [], "語彙にないラベル"
    asked = label
    f, t, label, fix = check_end_kinds(f, t, label, concepts, rel_by_label, wide, allow_swap=not fragments)
    if fix == "other":
        if fragments and check_end_kinds(f, t, asked, concepts, rel_by_label, wide)[3] == "swapped":
            return {}, [], "端の種類が逆向き"
        return {}, [], "端の種類が合わない"
    ends = [concepts[f]["label"], concepts[t]["label"]]
    end_concepts = [concepts[f], concepts[t]]
    rel = {"from": f, "to": t, "label": label}
    reasons = []
    if j["slide_found"]:
        page = (j["lecture"], j["page"])
        if page not in pages_allowed or page not in page_text:
            reason = "対に付けていないページ"
        elif fragments:
            reason = check_fragment_evidence(j["slide_evidence"], end_concepts, page_text[page], person_kinds,
                                             tuple(c["label"] for c in concepts.values()))
        else:
            reason = check_evidence(j["slide_evidence"], ends, page_text[page]) or \
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
            rel.update(origin="external", evidence=j["wiki_evidence"].strip(), evidence_url=article.get("url", ""),
                       evidence_title=article.get("title", ""))
    return rel, reasons, fix


def complete_relations(client, model, vocab, graph, pages, index, wiki, log=print,
                       max_pairs: int = COMPLETION_MAX_PAIRS, workers: int | None = None) -> dict:
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
    LLM の判定は COMPLETION_BATCH 対ずつ、workers 本（既定 EXTRACT_WORKERS）で並列に呼ぶ（#107）。
    呼び出しの間隔は common が全体で守る。答えは対の順に照合するので、終わる順で結果は変わらない。
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
        # 同時に数本で引く（#107。本数と User-Agent は rag.Wiki.lookup_many が決める）
        ids = sorted({x for p in pairs for x in (p["a"], p["b"])})
        found = wiki.lookup_many([concepts[cid]["label"] for cid in ids])
        articles = {cid: found.get(concepts[cid]["label"]) for cid in ids}
    record["wiki_found"] = sum(1 for a in articles.values() if a)
    record["wiki_lookups"] = len(articles)

    pair_records = [
        {"a": p["a"], "b": p["b"], "pages": p["page_info"],
         "wiki": [{"term": concepts[cid]["label"], "title": (articles.get(cid) or {}).get("title", ""),
                   "url": (articles.get(cid) or {}).get("url", "")} for cid in (p["a"], p["b"]) if cid in articles],
         "judgement": None, "result": "no_answer", "reason": "", "relation": None}
        for p in pairs
    ]
    wide = concept_label_kinds(vocab)
    rel_text = "\n".join(
        _label_line(r, wide)
        for r in vocab["relations"] if r["label"] in labels
    )
    dropped = collections.Counter()
    adjusted = collections.Counter()
    added: list[dict] = []
    batches: list[tuple[int, list[dict], list[str], str]] = []   # (先頭の番号, 対, [W番号] → id, プロンプト)

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
        batches.append((start, batch, wiki_ids, COMPLETION_USER.format(
            course=vocab.get("course", ""), relations=rel_text,
            pages=pages_block, wiki=wiki_block, pairs=pairs_block)))

    def ask(b: tuple) -> dict:
        return call_json(client, model=model, system=COMPLETION_SYSTEM, schema=COMPLETION_SCHEMA, user=b[3])

    answers: list[dict] = []
    if batches:
        with ThreadPoolExecutor(max_workers=max(1, min(workers or EXTRACT_WORKERS, len(batches)))) as ex:
            answers = list(ex.map(ask, batches))
    api_calls = len(batches)

    for (start, batch, wiki_ids, _), data in zip(batches, answers):
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
            if claimed == "none" or not j["label"].strip():
                dropped["関係なし"] += 1
                rec["result"] = "none"
                continue
            if {j["from"].strip(), j["to"].strip()} != {p["a"], p["b"]}:
                reject("対に無い id")
                continue
            rel, reasons, fix = verify_judgement(j, set(p["pages"]), wiki_ids, articles, concepts, page_text,
                                                 labels, rel_by_label, wide, person_kinds)
            if fix in ("語彙にないラベル", "端の種類が合わない"):
                reject(fix)
                continue
            if fix == "swapped":
                adjusted["向きを直した"] += 1
            elif fix == "widened":
                adjusted["端の種類を広げて残した"] += 1
            if "origin" in rel and rel["origin"] == "external" and not rel.get("source"):
                shared = sorted(_pages_of(concepts[rel["from"]]) & _pages_of(concepts[rel["to"]]))
                lec, pg = (shared or p["pages"])[0]
                rel["source"] = {"lecture": lec, "page": pg}
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



# ------------------------------------------------------------------ つながらない概念をつなぐ（#127）
#
# 利用者の指示: すべての概念に必ず 1 本以上の関係を作る。2 段で行う。
#   1 段目（LLM）: つながらない概念ごとに、資料の位置と索引で選んだ候補から、いちばん確かな相手・ラベル・向き・evidence を
#                 1 つ答えさせる。照合は補完と同じ（verify_judgement）。
#   2 段目（コード）: それでも残った概念を、同じページ → 前後のページ → 同じ回 → 索引で近いページの順に選んだ相手と、
#                 固定ラベル「関連」（RELATED_LABEL、origin: "cooccurrence"）でつなぐ。evidence は持たない。

CONNECT_SYSTEM = """あなたは大学の授業資料の概念どうしの関係を、資料のページと百科事典（Wikipedia）の記事の記述で確かめる専門家です。
関係の両端には与えられた概念の id だけを使い、関係ラベルには与えられたラベルだけを使います。"""

CONNECT_USER = """授業「{course}」で、まだどの概念とも関係が無い概念があります。各概念について、候補の中から
いちばん確かな関係を 1 つ選んでください。

関係ラベル（これ以外は使わない）:
{relations}

=== 資料のページ（[第N回 pM] の形で示す） ===
{pages}

=== Wikipedia の記事の冒頭（[W番号] の形で示す） ===
{wiki}

=== つなぐ概念と、相手の候補 ===
{targets}

各概念について 1 件答える（target にその概念の番号）。確かな関係が無い概念は、label を空にし、
slide_found と external_found を false にする（その概念は別の方法でつなぐので、無理に関係を作らない）。
- partner: 候補の id から 1 つ。資料の記述でいちばん確かに結びつく相手を選ぶ
- from・to・label: target の概念と partner の関係を、上のラベルの定義で表す。from と to は 2 つの id で、ラベルの定義が決める向きに従う。
  そのラベルの例（「A → B」の A が from、B が to）と同じ向きで書く。どのラベルにも当てはまらなければ label を空にする
{structure_rules}
- ラベルは、定義の from・to の種類と意味の両方に合うときだけ使う。種類だけ合わせて、意味の違うラベルを選ばない
- 作品や登場人物を論じた人を、作品の作者や登場人物に対する批判・提唱の主体にしない（論じた人とその作品・人物の関係は、
  資料にその関係を表す記述とラベルがあるときだけ）
- ラベルの【向き】に合う向きで書けない関係は答えない（向きを入れ替えて種類に合わせない）
- 例題・練習問題の題材（数値の例に出てくる人・物・病気・データなど）は、手法の具体例であって、手法が使う概念ではない。
  題材を相手にした関係を作らない
- 相手を選ぶ前に、各候補について、二つの概念の関係を書いた文が資料のページにあるかを確かめる。
  文が無い候補は選ばず、文がある別の候補を選ぶ（資料にある候補を、Wikipedia だけにある候補より先にする）
- 資料の根拠: 資料のページに二つの概念を結びつける記述があれば slide_found を true にし、lecture と page にそのページを書く。
  slide_evidence には、そのページの本文から写した断片を 1 つか 2 つ書く（2 つのときは「 ... 」で区切る。合わせて 80 字以内）。
  断片はそれぞれページ本文の連続した文字をそのまま写す（言い換えない・記号や数式を書き直さない・語を省かない）。
  断片の少なくとも 1 つには、どちらかの概念の名前を入れる。もう一方の名前は同じページにあればよい
- Wikipedia の根拠: 記事の本文に二つの概念を結びつける記述があれば external_found を true にし、wiki にその記事の番号を、
  wiki_evidence にその箇所を記事の本文からそのまま 80 字以内で抜き出す。引用には二つの概念の名前を両方入れる
- 引用は本文の文字をそのまま写す。あなた自身の知識だけで関係を決めない
- 使わない欄は、文字列なら空、数なら 0、真偽なら false にする
"""
CONNECT_USER = CONNECT_USER.replace("{structure_rules}", STRUCTURE_RULES)

CONNECT_SCHEMA = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target": {"type": "integer"},
                    "partner": {"type": "string"},
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
                "required": ["target", "partner", "from", "to", "label", "slide_found", "lecture", "page",
                             "slide_evidence", "external_found", "wiki", "wiki_evidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["answers"],
    "additionalProperties": False,
}

CONNECT_BATCH = int(os.environ.get("QUIZMAP_CONNECT_BATCH", "12"))        # 1 回の呼び出しで判定する概念の数
CONNECT_CANDIDATES = 8          # 1 つの概念に付ける相手の候補の数
CONNECT_OWN_PAGES = 2           # 1 つの概念に付ける、その概念が出るページの数
CONNECT_SEARCH_K = 3            # 索引で近いページを取る数（候補の相手と、資料のページに 1 つ）
COOCCURRENCE = "cooccurrence"   # 2 段目の関係の origin
CONNECT_VIA = ("same_page", "adjacent_page", "same_lecture", "vector", "fallback", "over_cap")
CONNECT_MAX_RECEIVED = int(os.environ.get("QUIZMAP_CONNECT_MAX_RECEIVED", "3"))   # 1 つの概念が受ける「関連」の上限


def _degree(relations: list[dict]) -> collections.Counter:
    deg = collections.Counter()
    for r in relations:
        deg[r["from"]] += 1
        deg[r["to"]] += 1
    return deg


def concept_vectors(index, concepts: dict[str, dict]) -> dict:
    """概念ごとの、その概念が出るページの索引ベクトルの平均（長さ 1）。埋め込みは呼ばない（#127）。
    索引が無ければ空。索引に無いページだけの概念は入れない。"""
    import numpy as np

    if index is None:
        return {}
    pos = {(p["lecture"], p["page"]): i for i, p in enumerate(index.pages)}
    vectors = np.asarray(index.vectors, dtype=np.float32)
    out = {}
    for cid, c in concepts.items():
        rows = [pos[p] for p in sorted(_pages_of(c)) if p in pos]
        if rows:
            q = vectors[rows].mean(axis=0)
            n = float(np.linalg.norm(q))
            if n:
                out[cid] = q / n
    return out


def similarities(cid: str, vecs: dict) -> dict[str, float] | None:
    """cid と各概念の内容の近さ（concept_vectors のコサイン類似度）。cid のベクトルが無ければ None。"""
    if cid not in vecs:
        return None
    v = vecs[cid]
    return {o: float(w @ v) for o, w in vecs.items() if o != cid}


def link_groups(concepts: dict[str, dict], relations: list[dict]) -> dict[str, int]:
    """概念ごとの、つながりの組（#127）: 0 = 最大の連結成分、1 = ほかの連結成分で関係を持つ、2 = どの関係にも無い。"""
    adj: dict[str, set[str]] = collections.defaultdict(set)
    for r in relations:
        adj[r["from"]].add(r["to"])
        adj[r["to"]].add(r["from"])
    comp: dict[str, int] = {}
    sizes = []
    for c in concepts:
        if c in comp or c not in adj:
            continue
        stack, n = [c], 0
        comp[c] = len(sizes)
        while stack:
            x = stack.pop()
            n += 1
            for y in adj[x]:
                if y not in comp:
                    comp[y] = len(sizes)
                    stack.append(y)
        sizes.append(n)
    biggest = max(range(len(sizes)), key=lambda i: (sizes[i], -i)) if sizes else -1
    return {c: 2 if c not in comp else 0 if comp[c] == biggest else 1 for c in concepts}


def connect_candidates(cid: str, concepts: dict[str, dict], on_page: dict, deg: collections.Counter,
                       near_pages: list[dict] | None,
                       sim: dict[str, float] | None = None,
                       groups: dict[str, int] | None = None) -> list[tuple[str, str, tuple[int, int] | None]]:
    """つながらない概念 cid の相手の候補（#127）。LLM も埋め込みも呼ばない。

    同じページに出る概念 → 前後のページ → 同じ回 → 索引で近いページ（near_pages。search_by_own_pages の結果）に出る概念の順。
    各段の中は、sim（similarities）があれば内容の近い順。無ければ関係の少ない順（同じ回は、ページの近い順を先に）。
    それも同じなら id の順。関係の多い順にしないのは、相手が一部の概念に集まって星形になるのを避けるため。
    groups（link_groups）があれば、段の中ではその組の順（最大の連結成分 → ほかの関係を持つ概念 → つながらない概念）を
    先にする（つながらない概念どうしで小さな島を作らないため）。
    戻り値: [(相手の id, 段（CONNECT_VIA）, 一緒に出るページ または None)]（重複なし、全件）
    """
    own = sorted(_pages_of(concepts[cid]))
    seen = {cid}
    out = []

    def push(others, via, page_of):
        for o in others:
            if o not in seen:
                seen.add(o)
                out.append((o, via, page_of(o)))

    grp = (lambda o: groups.get(o, 2)) if groups is not None else (lambda o: 0)

    def by_deg(ids):
        if sim is not None:
            return sorted(ids, key=lambda o: (grp(o), -sim.get(o, -2.0), deg[o], o))
        return sorted(ids, key=lambda o: (grp(o), deg[o], o))
    same = {o: p for p in own for o in on_page.get(p, [])}
    push(by_deg(same), "same_page", lambda o: same[o])
    adj = {o: p for (l, n) in own for p in ((l, n - 1), (l, n + 1)) for o in on_page.get(p, [])}
    push(by_deg(adj), "adjacent_page", lambda o: None)
    lectures = {l for l, _ in own} or set(concepts[cid].get("lectures") or [])
    dist = {}
    for (l, n), ids in on_page.items():
        if l in lectures:
            d = min((abs(n - m) for ll, m in own if ll == l), default=0)
            for o in ids:
                dist[o] = min(dist.get(o, d), d)
    push(by_deg(dist) if sim is not None else sorted(dist, key=lambda o: (grp(o), dist[o], deg[o], o)),
         "same_lecture", lambda o: None)
    for h in near_pages or []:
        push(by_deg(on_page.get((h["lecture"], h["page"]), [])), "vector", lambda o: None)
    return out


def connect_isolated(client, model, vocab, graph, pages, index, wiki, log=print,
                     batch_size: int = CONNECT_BATCH, workers: int | None = None, use_llm: bool = True,
                     max_received: int = CONNECT_MAX_RECEIVED) -> dict:
    """どの関係にもつながらない概念を、すべて 1 本以上の関係でつなぐ（#127）。新しい graph を返す（ファイルは書かない）。

    1 段目（use_llm かつ client があるとき）: つながらない概念を batch_size 個ずつ LLM に判定させる。各概念に
      相手の候補（connect_candidates の先頭 CONNECT_CANDIDATES 個）と、その概念のページ・索引で近いページ・Wikipedia の冒頭を
      付け、相手・ラベル・向き・evidence を 1 つずつ答えさせる。照合は verify_judgement（補完と同じ）。
      足す関係は origin "slide" / "external"、added_by "connect"。
    2 段目（コード）: それでも残った概念ごとに、connect_candidates の順（段の中は内容の近い順）で、受けた「関連」が
      max_received 本未満の先頭の相手と RELATED_LABEL でつなぐ（星形を避ける）。段の中では、最大の連結成分の概念 →
      ほかの関係を持つ概念 → つながらない概念の順を先にする（小さな島を作らない）。
      origin "cooccurrence"、added_by "connect"、via に段、source は一緒に出るページ（無ければその概念のページ）。
      候補が尽きたら、ほかのすべての概念を内容の近い順（索引が無ければ関係の少ない順）に見る（via "fallback"）。
    概念が 2 つ以上あれば、つながらない概念は必ず 0 になる。stats.connect に数と記録を残す。
    index（SlideIndex）と wiki（Wiki）は None でもよい。
    """
    concepts = {c["id"]: c for c in graph["concepts"]}
    relations = [dict(r, origin=r.get("origin", "slide")) for r in graph["relations"]]
    page_text = {(p["lecture"], p["page"]): p["text"] for p in pages}
    labels = {r["label"] for r in vocab["relations"]} | {OTHER_LABEL}
    rel_by_label = {r["label"]: r for r in vocab["relations"]}
    person_kinds = {k["name"] for k in vocab["kinds"] if k.get("person") is True}
    wide = concept_label_kinds(vocab)

    on_page: dict[tuple[int, int], list[str]] = collections.defaultdict(list)
    for cid, c in concepts.items():
        for p in sorted(_pages_of(c)):
            on_page[p].append(cid)

    def isolated_now() -> list[str]:
        linked = {x for r in relations for x in (r["from"], r["to"])}
        return [cid for cid in concepts if cid not in linked]

    isolated = isolated_now()
    record = {"isolated_before": len(isolated), "stage1": {}, "stage2": {}, "isolated_after": 0}
    log(f"つながらない概念をつなぐ: {len(isolated)} 個")
    near: dict[str, list[dict] | None] = {}
    vecs = concept_vectors(index, concepts)
    if index is not None and isolated:
        found = search_by_own_pages(index, [_pages_of(concepts[cid]) for cid in isolated], k=CONNECT_SEARCH_K)
        near = dict(zip(isolated, found))

    # ---- 1 段目
    s1 = {"targets": 0, "api_calls": 0, "added_slide": 0, "added_external": 0, "dropped": {}, "adjusted": {},
          "records": []}
    if use_llm and client is not None and isolated:
        deg = _degree(relations)
        groups = link_groups(concepts, relations)
        cands = {cid: connect_candidates(cid, concepts, on_page, deg, near.get(cid),
                                         similarities(cid, vecs), groups)[:CONNECT_CANDIDATES]
                 for cid in isolated}
        targets = [cid for cid in isolated if cands[cid]]
        articles: dict[str, dict | None] = {}
        if wiki is not None and targets:
            got = wiki.lookup_many([concepts[cid]["label"] for cid in targets])
            articles = {cid: got.get(concepts[cid]["label"]) for cid in targets}
        rel_text = "\n".join(
            _label_line(r, wide)
            for r in vocab["relations"] if r["label"] != OTHER_LABEL)
        batches = []
        for start in range(0, len(targets), max(1, batch_size)):
            batch = targets[start:start + batch_size]
            shown: dict[str, list[tuple[int, int]]] = {}
            for cid in batch:
                pg = sorted(_pages_of(concepts[cid]))[:CONNECT_OWN_PAGES]
                pg += [(h["lecture"], h["page"]) for h in (near.get(cid) or [])[:1]]
                shown[cid] = [x for x in pg if x in page_text]
            page_keys = sorted({x for v in shown.values() for x in v})
            wiki_ids = [cid for cid in batch if articles.get(cid)]
            pages_block = "\n\n".join(f"[第{l}回 p{n}]\n{page_text[(l, n)][:PAGE_CHARS]}" for l, n in page_keys) \
                or "（なし）"
            wiki_block = "\n\n".join(f"[W{i + 1}] {articles[cid]['title']}\n{_wiki_text(articles[cid])}"
                                     for i, cid in enumerate(wiki_ids)) or "（なし）"

            def label_of(o):
                c = concepts[o]
                return f"{o}（{c['label']}／種類: {c['kind']}）"

            blocks = []
            for i, cid in enumerate(batch):
                c = concepts[cid]
                w = f"、記事 W{wiki_ids.index(cid) + 1}" if cid in wiki_ids else ""
                blocks.append(
                    f"{start + i + 1}. {label_of(cid)}{w}: {c.get('definition', '')}\n"
                    f"   ページ: {'、'.join(f'第{l}回 p{n}' for l, n in shown[cid]) or 'なし'}\n"
                    f"   候補: {' / '.join(label_of(o) for o, _, _ in cands[cid])}")
            batches.append((start, batch, wiki_ids, page_keys, CONNECT_USER.format(
                course=vocab.get("course", ""), relations=rel_text, other=OTHER_LABEL,
                pages=pages_block, wiki=wiki_block, targets="\n".join(blocks))))

        def ask(b):
            return call_json(client, model=model, system=CONNECT_SYSTEM, schema=CONNECT_SCHEMA, user=b[4])

        answers = []
        if batches:
            with ThreadPoolExecutor(max_workers=max(1, min(workers or EXTRACT_WORKERS, len(batches)))) as ex:
                answers = list(ex.map(ask, batches))
        dropped, adjusted = collections.Counter(), collections.Counter()
        recs = {cid: {"concept": cid, "candidates": [o for o, _, _ in cands[cid]], "answer": None,
                      "result": "no_answer", "reason": "", "relation": None} for cid in targets}
        for (start, batch, wiki_ids, page_keys, _), data in zip(batches, answers):
            done = set()
            for a in data["answers"]:
                n = a["target"] - 1
                if not start <= n < start + len(batch) or n in done:
                    dropped["番号が無い・重複"] += 1
                    continue
                done.add(n)
                cid = batch[n - start]
                rec = recs[cid]
                rec["answer"] = {k: a[k] for k in CONNECT_SCHEMA["properties"]["answers"]["items"]["properties"]}
                partner = a["partner"].strip()

                def reject(reason):
                    dropped[reason] += 1
                    rec.update(result="rejected", reason=reason)

                if not a["label"].strip() or not (a["slide_found"] or a["external_found"]):
                    dropped["関係なし"] += 1                 # 確かな関係が無いと答えた（#135。2 段目でつなぐ）
                    rec["result"] = "none"
                    continue
                if partner not in rec["candidates"]:
                    reject("候補に無い相手")
                    continue
                if {a["from"].strip(), a["to"].strip()} != {cid, partner}:
                    reject("相手と端が合わない")
                    continue
                if any(cid in (r["from"], r["to"]) for r in relations):
                    rec.update(result="already_linked")        # 同じ組の前の答えで、相手の側としてつながった
                    continue
                rel, reasons, fix = verify_judgement(
                    a, set(page_keys), wiki_ids, articles,
                    concepts, page_text, labels, rel_by_label, wide, person_kinds, fragments=True)
                if fix in ("語彙にないラベル", "端の種類が合わない", "端の種類が逆向き"):
                    reject(fix)
                    continue
                if fix in ("swapped", "widened"):
                    adjusted[{"swapped": "向きを直した", "widened": "端の種類を広げて残した"}[fix]] += 1
                if "origin" not in rel:
                    reject(reasons[0] if reasons else "根拠なし")
                    continue
                if rel["origin"] == "external":
                    shared = sorted(_pages_of(concepts[rel["from"]]) & _pages_of(concepts[rel["to"]]))
                    own = sorted(_pages_of(concepts[cid]))
                    lec, pg = (shared or own or [(0, 0)])[0]
                    rel["source"] = {"lecture": lec, "page": pg}
                rel["added_by"] = "connect"
                relations.append(rel)
                s1["added_" + rel["origin"]] += 1
                rec.update(result="added", relation=rel)
            dropped["答えの無い概念"] += len(batch) - len(done)
        s1.update(targets=len(targets), api_calls=len(batches), dropped={k: v for k, v in dropped.items() if v},
                  adjusted=dict(adjusted), records=list(recs.values()))
        log(f"つなぐ 1 段目: {len(targets)} 個を LLM {len(batches)} 回で判定、"
            f"slide {s1['added_slide']} / external {s1['added_external']} 本を足した")
    record["stage1"] = s1

    # ---- 2 段目
    # 相手は候補の順（内容の近い順）。1 つの概念が受ける「関連」は max_received 本までで、達した概念は次の候補に回す。
    # 候補が尽きたら、ほかのすべての概念を同じ順（内容の近い順、索引が無ければ関係の少ない順）で見る（fallback）。
    # それでも上限で選べないとき（概念がとても少ない）だけ、上限を超えて受けの少ない概念とつなぐ（over_cap）
    added2, by_via = [], collections.Counter()
    received = collections.Counter()
    for cid in isolated_now():
        if any(cid in (r["from"], r["to"]) for r in relations):
            continue                                  # この段で、ほかの概念の相手としてつながった
        deg = _degree(relations)
        sim = similarities(cid, vecs)
        groups = link_groups(concepts, relations)
        cands = connect_candidates(cid, concepts, on_page, deg, near.get(cid), sim, groups)
        listed = {o for o, _, _ in cands}
        key = (lambda o: (groups[o], -sim.get(o, -2.0), deg[o], o)) if sim is not None \
            else (lambda o: (groups[o], deg[o], o))
        cands += [(o, "fallback", None) for o in sorted((o for o in concepts if o != cid and o not in listed), key=key)]
        if not cands:
            continue
        pick = next((c for c in cands if received[c[0]] < max_received), None)
        if pick is None:
            o = min(cands, key=lambda c: (received[c[0]], cands.index(c)))[0]
            pick = (o, "over_cap", None)
        partner, via, page = pick
        received[partner] += 1
        own = sorted(_pages_of(concepts[cid]))
        src = page or (own[0] if own else (sorted(_pages_of(concepts[partner])) or [(0, 0)])[0])
        rel = {"from": cid, "to": partner, "label": RELATED_LABEL, "origin": COOCCURRENCE,
               "source": {"lecture": src[0], "page": src[1]}, "added_by": "connect", "via": via}
        relations.append(rel)
        added2.append(rel)
        by_via[via] += 1
    record["stage2"] = {"added": len(added2), "by_via": {v: by_via[v] for v in CONNECT_VIA if by_via[v]},
                        "max_received": max(received.values(), default=0), "partners": len(received),
                        "similarity": bool(vecs)}
    record["isolated_after"] = len(isolated_now())
    log(f"つなぐ 2 段目: 「{RELATED_LABEL}」で {len(added2)} 本（"
        + "、".join(f"{v} {n}" for v, n in record["stage2"]["by_via"].items()) + f"）。"
        f"つながらない概念 {record['isolated_before']} → {record['isolated_after']}")

    n_other = sum(1 for r in relations if r["label"] == OTHER_LABEL)
    stats = dict(graph.get("stats", {}))
    stats.update(
        relations=len(relations),
        other_count=n_other,
        other_rate=round(n_other / len(relations), 4) if relations else 0.0,
        relations_by_label=dict(collections.Counter(r["label"] for r in relations).most_common()),
        isolated_concepts=record["isolated_after"],
        connect=record,
    )
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
    # 『』の題名を含む名前（#157）: 1) 落とした本と同じ題名を含む概念（著者名付き）は本として落とす
    # 2) 残りの『』の題名を含む概念は、授業が作品の内容を論じているかを LLM に確かめ、論じていなければ本として落とす
    titled_record = {"same_title": [], "checked": [], "api_calls": 0}
    if cited_kinds:
        same = same_title_ids(concepts, removed_labels)
        titled_record["same_title"] = [concepts[cid]["label"] for cid in same]
        cands = [cid for cid in concepts if cid not in same and _TITLE_IN_RE.search(concepts[cid]["label"])]
        checked, calls = check_titled_works(client, model, course, {cid: concepts[cid] for cid in cands}, lectures, log) \
            if cands and client is not None else ({}, 0)
        titled_record.update(checked=[{"label": concepts[cid]["label"], **v} for cid, v in checked.items()], api_calls=calls)
        drop_ids = set(same) | {cid for cid, v in checked.items() if not v["discussed"]}
        removed_labels += [concepts[cid]["label"] for cid in concepts if cid in drop_ids]
        concepts = {cid: c for cid, c in concepts.items() if cid not in drop_ids}
        n_cited = n_before - len(concepts)
    if cited_kinds:
        # 概念と関係を同時に取るので、落とした概念を端にする関係もここで落とす
        n_rel = len(relations)
        relations = [r for r in relations if r["from"] in concepts and r["to"] in concepts]
        if n_rel - len(relations):
            dropped["端が出典の文献"] = n_rel - len(relations)
        log(f"出典・参考の文献・作品の種類（{'・'.join(cited_kinds)}）の概念と、題名でそれと分かる概念 {n_cited} 件と、"
            f"それを端にする関係 {n_rel - len(relations)} 本を落とした: " + " / ".join(removed_labels))
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
    dom = kind_dominance(kind_counts)
    if dom["warning"]:
        log(f"警告: 概念の {dom['share']:.0%} が種類「{dom['kind']}」に入っている。語彙の種類が粗い（語彙を作り直すと直る見込み）")
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
        "cited_titles": titled_record,                                  # 『』の題名を含む名前の扱い（#157）
        "unmatched_kind_examples": unmatched_kind[:20],
        "extract_chunks": concept_info["chunks"],
        "extract_calls": concept_info["calls"],
        "extract_pages_per_call": concept_info["pages_per_call"],
        "extract_chunk_chars": concept_info["chunk_chars"],
        "extract_max_pages": concept_info["max_pages"],
        "extract_overlap": concept_info["overlap"],
        "extract_workers": concept_info["workers"],
        "merged_mentions": concept_info["merged_mentions"],
        "merged_variants": concept_info["merged_variants"],
        "merged_variant_examples": concept_info["merged_variant_examples"],
        "merged_people": concept_info.get("merged_people", []),
        "titles_not_person": concept_info.get("titles_not_person", []),  # 『』の名前で人物の種類だったもの（その他にした）       # 同じ人物の別の書き方をまとめた組（姓だけ・頭文字）
        "concepts_listed_by_lecture": concept_info["listed_by_lecture"],
        "new_concepts_by_lecture": concept_info["new_by_lecture"],
        "label_fixed_count": len(concept_info["label_fixes"]),
        "label_fix_by_how": concept_info["label_fix_by_how"],
        "label_fixes": concept_info["label_fixes"],
        "surface_ask_calls": concept_info["surface_ask_calls"],
        "surface_asked": concept_info["surface_asked"],
        "surface_ask_resolved": concept_info["surface_ask_resolved"],
        "surface_collisions_relocated": concept_info["surface_collisions_relocated"],
        "surface_collisions_without": concept_info["surface_collisions_without"],
        "without_surface_count": len(concept_info["without_surface"]),
        "without_surface_examples": concept_info["without_surface"][:SURFACE_EXAMPLES],
        "isolated_concepts": n_isolated,
        "relations_by_label": dict(label_counts.most_common()),
        "concepts_by_kind": dict(kind_counts.most_common()),
        "kind_dominance": kind_dominance(kind_counts),                  # 1 つの種類に概念が偏っているか（#150）
        "dropped": dropped,
        "dropped_relations": concept_info["dropped_relations"],   # 落とした関係（理由・端の名前・ページ、#114）
        "loose_evidence": concept_info["loose_evidence"],         # 括弧書き・記号を除いて照合が通った関係の数
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
