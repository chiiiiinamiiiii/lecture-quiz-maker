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
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from quizmap.common import OTHER_LABEL, Lecture, call_json, lecture_as_prompt_text, normalize_text, write_json

# ------------------------------------------------------------------ 概念抽出

CONCEPT_SYSTEM = """あなたは大学の授業資料から、学習者が理解すべき「概念」を漏れなく取り出す専門家です。
出力は日本語。与えられた種類（kind）だけを使います。"""

CONCEPT_USER = """授業「{course}」の第{lecture}回の資料です（ページ番号付き）。
この回に出てくる概念をすべて取り出してください。

概念の種類（これ以外の kind は使わない。どれにも当てはまらなければ「{other}」）:
{kinds}

{known_block}
守ること:
- 出てくる概念はすべて挙げる。1 度しか出てこない概念も挙げる。数の上限はない
- 一方で、資料に書かれていない概念を足さない
- label は資料中の表記に合わせた短い日本語（人名は姓名、立場は正式名称）
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


def clean_id(raw: str, fallback: str) -> str:
    s = _ID_RE.sub("_", raw.strip().lower()).strip("_")
    return s or fallback


def extract_concepts(client, model: str, course: str, vocab: dict, lectures: list[Lecture], log=print):
    """回ごとに順番に呼ぶ。既出の概念一覧を渡して label/id を揃えさせる。"""
    kinds_text = "\n".join(f"- {k['name']}: {k['definition']}（例: {k['example']}）" for k in vocab["kinds"])
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
            client, model=model, system=CONCEPT_SYSTEM, schema=CONCEPT_SCHEMA,
            user=CONCEPT_USER.format(
                course=course, lecture=lec.number, kinds=kinds_text, other=OTHER_LABEL,
                known_block=known_block, text=lecture_as_prompt_text(lec),
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


def extract_relations(client, model: str, course: str, vocab: dict, lectures: list[Lecture],
                      concepts: dict[str, dict], workers: int, log=print) -> tuple[list[dict], dict]:
    """概念 id を固定して、回ごとに関係を出させる（回は独立なので並列）。"""
    labels = {r["label"] for r in vocab["relations"]} | {OTHER_LABEL}
    rel_text = "\n".join(
        f"- {r['label']}: {r['definition']}（例: {r['example']}）"
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
                if not 1 <= r["page"] <= lec.page_count:
                    dropped["ページ番号が範囲外"] += 1
                    continue
                page = r["page"]
                reason = check_evidence(r.get("evidence", ""), [concepts[f]["label"], concepts[t]["label"]],
                                        lec.pages[page - 1])
                if reason:
                    dropped[reason] += 1
                    continue
                # evidence は検証に使うだけで保存しない（講義資料の本文を graph.json に増やさない）
                candidates.append({"from": f, "to": t, "label": label,
                                   "source": {"lecture": lec.number, "page": page}})
                kept += 1
            log(f"関係 第{lec.number}回: {len(data['relations'])} 件 → 候補 {kept}")
    relations, merged = resolve_pair_conflicts(candidates)
    dropped.update(merged)
    log("同じ概念対の整理: " + (", ".join(f"{k} {v}" for k, v in merged.items()) or "なし")
        + f" → 関係 {len(relations)}")
    return relations, dict(dropped)


def resolve_pair_conflicts(candidates: list[dict]) -> tuple[list[dict], collections.Counter]:
    """1 つの概念対（向きを問わない）に、1 つのラベルと 1 つの向きだけを残す（Issue #12）。

    回ごとに独立に抽出するため、同じ対に別のラベルや逆向きの関係が付くことがある。
    LLM は呼ばず、資料中で何度その関係が書かれたか（支持数）で決める。
    1. 同じ対に「その他」以外のラベルがあれば、「その他」を落とす
    2. ラベルが 2 つ以上残れば、支持数（両向きの合計）の多いラベルを残す
    3. そのラベルが両向きにあれば、支持数の多い向きを残す
       （対立のように向きの無いラベルでも、両向きは同じ関係の重複なので 1 本にしてよい）
    同点は先に出たもの（若い回）を残す。残した関係の source は、その (from, to, label) が最初に出た箇所。
    candidates は出現順に並んでいること。完全に同じ (from, to, label) も支持数として数える。
    """
    dropped = collections.Counter()
    first: dict[tuple, dict] = {}                     # (from, to, label) -> 最初に出た関係
    support = collections.Counter()                   # (from, to, label) -> 出た回数
    order: dict[tuple, int] = {}                      # (from, to, label) -> 最初に出た位置
    for i, r in enumerate(candidates):
        key = (r["from"], r["to"], r["label"])
        support[key] += 1
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
            named = [k for k in keys if k[2] != OTHER_LABEL]
            if named and len(named) < len(keys):
                dropped["同じ対の「その他」"] += len(keys) - len(named)
                keys = named
        if len(keys) > 1:
            label_support = collections.Counter()
            label_order: dict[str, int] = {}
            for k in keys:
                label_support[k[2]] += support[k]
                label_order[k[2]] = min(label_order.get(k[2], order[k]), order[k])
            best_label = min(label_support, key=lambda l: (-label_support[l], label_order[l]))
            best = min((k for k in keys if k[2] == best_label), key=lambda k: (-support[k], order[k]))
            dropped["同じ対の食い違い"] += len(keys) - 1
            keys = [best]
        kept.append(keys[0])

    kept.sort(key=order.__getitem__)
    return [dict(first[k]) for k in kept], dropped


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
                   step=lambda name: None) -> dict:
    """抽出して out_dir に pages.json / graph.json / slides/ を書き、graph を返す。"""
    course = vocab.get("course", "")
    total_pages = sum(l.page_count for l in lectures)
    write_json(out_dir / "pages.json", [
        {"lecture": lec.number, "page": i, "text": page}
        for lec in lectures for i, page in enumerate(lec.pages, start=1)
    ])

    step("concepts")
    concepts, unmatched_kind = extract_concepts(client, model, course, vocab, lectures, log=log)
    step("relations")
    relations, dropped = extract_relations(client, model, course, vocab, lectures, concepts, workers, log=log)
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
        "unmatched_kind_examples": unmatched_kind[:20],
        "relations_by_label": dict(label_counts.most_common()),
        "concepts_by_kind": dict(kind_counts.most_common()),
        "dropped": dropped,
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
