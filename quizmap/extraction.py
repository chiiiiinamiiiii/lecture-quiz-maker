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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from quizmap.common import OTHER_LABEL, Lecture, call_json, lecture_as_prompt_text, normalize_text, write_json
from quizmap.vocab import kind_examples, language_name, language_of, language_rule

# ------------------------------------------------------------------ 概念抽出

CONCEPT_SYSTEM = """あなたは大学の授業資料から、学習者が理解すべき「概念」を漏れなく取り出す専門家です。
{language_rule}与えられた種類（kind）だけを使います。"""

CONCEPT_USER = """授業「{course}」の第{lecture}回の資料です（ページ番号付き）。
この回に出てくる概念をすべて取り出してください。

概念の種類（これ以外の kind は使わない。どれにも当てはまらなければ「{other}」）:
{kinds}

{known_block}
守ること:
- 出てくる概念はすべて挙げる。1 度しか出てこない概念も挙げる。数の上限はない
- スライドに出典・参考文献・引用元として書かれているだけの書名・報告書名・論文名は、概念として挙げない。
  その本の内容として説明されている概念や、著者の人物を挙げる
- 本の題名が、その本の主張する考え方の名前にもなっている場合（例: ある本の題名と同じ名前の学説）は、本とは別に、
  その考え方を概念として挙げる。その種類は【出典の文献】の種類にしない
- 授業の中で筋や内容が論じられている作品（劇・小説・報告書など）は、出典として挙がっているだけの本と区別する。
  作品そのものを【出典の文献】ではない種類で挙げ、内容を表す概念（その作品の中の対立や原則など）も挙げる
- 一方で、資料に書かれていない概念を足さない
- label は資料中の表記に合わせた短い{language}（人名は姓名、立場は正式名称）
- id は label をローマ字にした snake_case の ASCII（例: kant, gimu_ron）。既出の概念は既出の id と label をそのまま使う
- definition は資料の記述に基づいて 1〜2 文
- pages はその概念が登場するこの回のページ番号（複数可）。必ず 1 つ以上入れる

=== 資料 ===
{text}
"""

CONCEPT_SCHEMA = {
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
        }
    },
    "required": ["concepts"],
    "additionalProperties": False,
}

# ------------------------------------------------------------------ 関係抽出

RELATION_SYSTEM = """あなたは大学の授業資料から、概念どうしの「関係」を取り出す専門家です。
関係の両端には与えられた概念 id だけを使い、関係ラベルには与えられたラベルだけを使います。"""

RELATION_USER = """授業「{course}」の第{lecture}回の資料です（ページ番号付き）。
資料に書かれている概念どうしの関係をすべて取り出してください。

使ってよい概念（id: label）。これ以外の id は使わない:
{concepts}

関係ラベル（これ以外は使わない。どれにも当てはまらなければ「{other}」）:
{relations}

守ること:
- 資料に明示的に書かれている関係だけを挙げる。一般知識から補わない
- from と to は上の id から選ぶ。同じ id を両端にしない
- label はラベルの定義に従って選ぶ。無理に当てはめず、合わなければ「{other}」にする
- from と to はラベルの定義が決める向きに従う。逆向きの関係ならば from と to を入れ替える
- from と to の概念の種類が、ラベルの【向き】に書かれた種類でなければ、そのラベルは使わない
- A が B の定義そのもの、または A と B が同じものの言い換えである場合、それに合うラベルが無ければ「{other}」にする。ほかのラベルに押し込まない
- page はその関係が書かれているこの回のページ番号
- evidence には、その関係が書かれている箇所を page のページ本文からそのまま 40 字以内で抜き出す。抜き出せない関係は出さない
- 同じページに二つの概念が出てくるだけでは関係にしない。二つの概念を結びつける記述があるときだけ挙げる
- 1 度しか書かれていない関係も挙げる。数の上限はない

=== 資料 ===
{text}
"""

RELATION_SCHEMA = {
    "type": "object",
    "properties": {
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
        }
    },
    "required": ["relations"],
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


def extract_concepts(client, model: str, course: str, vocab: dict, lectures: list[Lecture], log=print):
    """回ごとに順番に呼ぶ。既出の概念一覧を渡して label/id を揃えさせる。"""
    lang = language_of(vocab, (page for lec in lectures for page in lec.pages))
    system = CONCEPT_SYSTEM.replace("{language_rule}", language_rule(lang))
    kinds_text = "\n".join(
        f"- {k['name']}{'【出典の文献】' if k.get('cited_work') is True else ''}: {k['definition']}"
        f"（例: {'、'.join(kind_examples(k))}）" for k in vocab["kinds"]
    )
    kind_names = {k["name"] for k in vocab["kinds"]}
    concepts: dict[str, dict] = {}          # id -> concept
    by_label: dict[str, str] = {}           # label -> id
    unmatched_kind: list[dict] = []         # vocab のどの kind にも当てはまらなかった概念

    for lec in lectures:
        if concepts:
            known = "\n".join(f"- {c['id']}: {c['label']}" for c in concepts.values())
            known_block = f"既出の概念（この回にも出てくるなら同じ id と label で挙げること）:\n{known}\n"
        else:
            known_block = ""
        data = call_json(
            client, model=model, system=system, schema=CONCEPT_SCHEMA,
            user=CONCEPT_USER.format(
                course=course, lecture=lec.number, kinds=kinds_text, other=OTHER_LABEL,
                known_block=known_block, text=lecture_as_prompt_text(lec), language=language_name(lang),
            ),
        )
        new = 0
        for i, c in enumerate(data["concepts"]):
            label = c["label"].strip()
            if not label:
                continue
            pages = sorted({p for p in c["pages"] if 1 <= p <= lec.page_count}) or [1]
            kind = c["kind"].strip()
            if kind != OTHER_LABEL and kind not in kind_names:
                # vocab にも「その他」にもない kind を付けてきた。落とさず「その他」に寄せ、件数と例を報告する
                if label not in by_label:
                    unmatched_kind.append({"label": label, "kind_given": kind, "lecture": lec.number})
                kind = OTHER_LABEL
            cid = by_label.get(label)
            if cid is None:
                cid = clean_id(c["id"], f"c{len(concepts) + 1:03d}")
                while cid in concepts:            # 別 label なのに id が衝突した
                    cid += "_"
                concepts[cid] = {
                    "id": cid, "label": label, "kind": kind,
                    "definition": c["definition"].strip(),
                    "sources": [], "lectures": [],
                }
                by_label[label] = cid
                new += 1
            entry = concepts[cid]
            for p in pages:
                src = {"lecture": lec.number, "page": p}
                if src not in entry["sources"]:
                    entry["sources"].append(src)
            if lec.number not in entry["lectures"]:
                entry["lectures"].append(lec.number)
        log(f"概念 第{lec.number}回: {len(data['concepts'])} 件（新規 {new}）→ 累計 {len(concepts)}")
    return concepts, unmatched_kind


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


def extract_relations(client, model: str, course: str, vocab: dict, lectures: list[Lecture],
                      concepts: dict[str, dict], workers: int, log=print,
                      end_kind_mismatch: str = END_KIND_MISMATCH_DEFAULT) -> tuple[list[dict], dict, dict]:
    """概念 id を固定して、回ごとに関係を出させる（回は独立なので並列）。

    end_kind_mismatch: 両端の種類がラベルの from_kinds・to_kinds に合わない関係の扱い。
      "other" = ラベルを「その他」にして残す / "drop" = 落とす（#49）
    """
    if end_kind_mismatch not in END_KIND_MISMATCH_POLICIES:
        raise ValueError(f"end_kind_mismatch は {END_KIND_MISMATCH_POLICIES} のどれか: {end_kind_mismatch!r}")
    labels = {r["label"] for r in vocab["relations"]} | {OTHER_LABEL}
    rel_by_label = {r["label"]: r for r in vocab["relations"]}
    adjusted = collections.Counter()      # 落とさずに直した数（dropped とは別に数える）
    rel_text = "\n".join(
        f"- {r['label']}: {r['definition']}{_direction_text(r)}（例: {r['example']}）"
        for r in vocab["relations"] if r["label"] != OTHER_LABEL
    )
    concept_text = "\n".join(f"- {c['id']}: {c['label']}" for c in concepts.values())

    def one(lec: Lecture):
        return lec, call_json(
            client, model=model, system=RELATION_SYSTEM, schema=RELATION_SCHEMA,
            user=RELATION_USER.format(
                course=course, lecture=lec.number, concepts=concept_text,
                relations=rel_text, other=OTHER_LABEL, text=lecture_as_prompt_text(lec),
            ),
        )

    candidates: list[dict] = []
    dropped = collections.Counter()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for lec, data in ex.map(one, lectures):        # ex.map は回の順を保つ
            kept = 0
            for r in data["relations"]:
                f, t, label = r["from"].strip(), r["to"].strip(), r["label"].strip()
                if f not in concepts or t not in concepts:
                    dropped["存在しない id"] += 1
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
                if not 1 <= r["page"] <= lec.page_count:
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
                kept += 1
            log(f"関係 第{lec.number}回: {len(data['relations'])} 件 → 候補 {kept}")
    relations, merged = resolve_pair_conflicts(candidates)
    dropped.update(merged)
    log("同じ概念対の整理: " + (", ".join(f"{k} {v}" for k, v in merged.items()) or "なし")
        + f" → 関係 {len(relations)}")
    if adjusted or dropped.get("端の種類が合わない→その他") or dropped.get("端の種類が合わない"):
        log(f"端の種類の照合（{end_kind_mismatch}）: 向きを直した {adjusted['向きを直した']}、"
            f"「その他」にした {dropped.get('端の種類が合わない→その他', 0)}、落とした {dropped.get('端の種類が合わない', 0)}")
    return relations, dict(dropped), dict(adjusted)


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

COMPLETION_SYSTEM = """あなたは大学の授業資料の概念どうしに、資料に書かれた関係、または百科事典で確かめられる関係があるかを判定する専門家です。
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

各対について 1 件ずつ答える。守ること:
- 根拠は次の順で探す。
  1. 対に付けた資料のページに、二つの概念を結びつける記述があれば origin を "slide" にする。lecture と page にそのページを、evidence にその箇所をページ本文からそのまま 40 字以内で抜き出す
  2. 資料に記述が無く、対に付けた Wikipedia の記事に二つの概念を結びつける記述があれば origin を "external" にする。wiki にその記事の番号を、evidence にその箇所を記事の本文からそのまま 60 字以内で抜き出す
  3. どちらにも結びつける記述が無ければ origin を "none" にする
- 同じページや同じ記事に二つの概念が出てくるだけでは関係にしない。一般知識から補わない
- label は上のラベルの定義に従って選ぶ。どのラベルにも当てはまらなければ origin を "none" にする
- from と to は対の 2 つの id。ラベルの定義が決める向きに従い、逆向きなら入れ替える。ほかの id は使わない
- 使わない欄は、文字列なら空、数なら 0 にする
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
                    "origin": {"type": "string", "enum": ["slide", "external", "none"]},
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "label": {"type": "string"},
                    "lecture": {"type": "integer"},
                    "page": {"type": "integer"},
                    "wiki": {"type": "integer"},
                    "evidence": {"type": "string"},
                },
                "required": ["pair", "origin", "from", "to", "label", "lecture", "page", "wiki", "evidence"],
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
       "judgement": LLM の答え（origin・label・from・to・lecture・page・wiki・evidence）, "result": "added" / "rejected" / "none" / "no_answer",
       "reason": 落とした理由, "relation": 足した関係}
    index（SlideIndex）と wiki（Wiki）は None でもよい。そのときは使える層だけで判定する。
    """
    concepts = {c["id"]: c for c in graph["concepts"]}
    relations = [dict(r, origin=r.get("origin", "slide")) for r in graph["relations"]]
    page_text = {(p["lecture"], p["page"]): p["text"] for p in pages}
    labels = {r["label"] for r in vocab["relations"]} - {OTHER_LABEL}
    rel_by_label = {r["label"]: r for r in vocab["relations"]}

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
            rec["judgement"] = {k: j[k] for k in ("origin", "label", "from", "to", "lecture", "page", "wiki", "evidence")}
            if j["origin"] == "none":
                dropped["関係なし"] += 1
                rec["result"] = "none"
                continue
            f, t, label = j["from"].strip(), j["to"].strip(), j["label"].strip()
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
            rel = {"from": f, "to": t, "label": label}
            if j["origin"] == "slide":
                page = (j["lecture"], j["page"])
                if page not in p["pages"]:
                    reject("対に付けていないページ")
                    continue
                reason = check_evidence(j["evidence"], ends, page_text[page])
                if reason:
                    reject(reason)
                    continue
                rel.update(source={"lecture": page[0], "page": page[1]}, origin="slide",
                           evidence=j["evidence"].strip())
            else:
                if not 1 <= j["wiki"] <= len(wiki_ids):
                    reject("記事の番号が無い")
                    continue
                cid = wiki_ids[j["wiki"] - 1]
                if cid not in (f, t):
                    reject("対に付けていない記事")
                    continue
                article = articles[cid]
                reason = check_external_evidence(j["evidence"], ends, article)
                if reason:
                    reject(reason)
                    continue
                shared = sorted(_pages_of(concepts[f]) & _pages_of(concepts[t]))
                lec, pg = (shared or p["pages"])[0]
                rel.update(source={"lecture": lec, "page": pg}, origin="external",
                           evidence=j["evidence"].strip(), evidence_url=article.get("url", ""),
                           evidence_title=article.get("title", ""))
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
                   workers: int = 4, dpi: int = 80, slides: bool = True, log=print,
                   step=lambda name: None, end_kind_mismatch: str = END_KIND_MISMATCH_DEFAULT) -> dict:
    """抽出して out_dir に pages.json / graph.json / slides/ を書き、graph を返す。"""
    course = vocab.get("course", "")
    total_pages = sum(l.page_count for l in lectures)
    write_json(out_dir / "pages.json", [
        {"lecture": lec.number, "page": i, "text": page}
        for lec in lectures for i, page in enumerate(lec.pages, start=1)
    ])

    step("concepts")
    concepts, unmatched_kind = extract_concepts(client, model, course, vocab, lectures, log=log)
    n_before = len(concepts)
    removed_labels = cited_work_labels(concepts, vocab)
    concepts, cited_kinds = remove_cited_works(concepts, vocab)
    n_cited = n_before - len(concepts)
    if cited_kinds:
        log(f"出典・参考の文献・作品の種類（{'・'.join(cited_kinds)}）の概念 {n_cited} 件を落とした（関係の抽出の前）: "
            + " / ".join(removed_labels))
    step("relations")
    relations, dropped, adjusted = extract_relations(client, model, course, vocab, lectures, concepts, workers, log=log,
                                                     end_kind_mismatch=end_kind_mismatch)
    if slides:
        step("slides")
        n_imgs = render_slides(lectures, out_dir / "slides", dpi)
        log(f"スライド画像 {n_imgs} 枚")

    n_other = sum(1 for r in relations if r["label"] == OTHER_LABEL)
    other_rate = n_other / len(relations) if relations else 0.0
    n_other_kind = sum(1 for c in concepts.values() if c["kind"] == OTHER_LABEL)
    other_kind_rate = n_other_kind / len(concepts) if concepts else 0.0
    label_counts = collections.Counter(r["label"] for r in relations)
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
        "relations_by_label": dict(label_counts.most_common()),
        "concepts_by_kind": dict(kind_counts.most_common()),
        "dropped": dropped,
        "adjusted": adjusted,
        "end_kind_mismatch": end_kind_mismatch,
        "model": model,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    write_json(out_dir / "graph.json", {
        "course": course,
        "concepts": list(concepts.values()),
        "relations": relations,
        "stats": stats,
    })

    graph = {
        "course": course,
        "concepts": list(concepts.values()),
        "relations": relations,
        "stats": stats,
    }
    write_json(out_dir / "graph.json", graph)
    log(f"概念 {len(concepts)} / 関係 {len(relations)} / 関係の「その他」率 {other_rate:.1%} / 種類の「その他」率 {other_kind_rate:.1%}")
    return graph
