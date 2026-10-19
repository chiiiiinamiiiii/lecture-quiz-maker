#!/usr/bin/env python
"""§5 抽出。PDF フォルダと vocab.json を受け取り、graph.json と slides/ のページ画像を出力する。

使い方:
    .venv/bin/python extract.py <pdf_dir> [--vocab data/vocab.json] [--out-dir data] [--model ...]

出力:
    <out-dir>/graph.json   concepts + relations + stats
    <out-dir>/pages.json   回・ページごとの正規化済みテキスト（ヒントや出題で再利用する）
    <out-dir>/slides/L{回}_p{ページ}.jpg

守っていること:
- NFKC 正規化と改行除去（quizmap/common.py）
- 概念数に上限を設けない。1 度しか出ない概念も切り捨てない
- 概念を先に全部出させ、その id だけを使って関係を書かせる
- 概念の sources にページ番号を必ず持たせる
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quizmap.common import (  # noqa: E402
    DEFAULT_MODEL, OTHER_LABEL, Lecture, call_json, lecture_as_prompt_text, load_lectures,
    make_client, read_json, write_json,
)

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
                },
                "required": ["from", "to", "label", "page"],
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


def extract_concepts(client, model: str, course: str, vocab: dict, lectures: list[Lecture]) -> dict[str, dict]:
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
        print(f"  第{lec.number:2d}回: {len(data['concepts']):3d} 件 (新規 {new:3d}) → 累計 {len(concepts)}")
    return concepts, unmatched_kind


def extract_relations(client, model: str, course: str, vocab: dict, lectures: list[Lecture],
                      concepts: dict[str, dict], workers: int) -> tuple[list[dict], dict]:
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

    relations: list[dict] = []
    seen: set[tuple] = set()
    dropped = collections.Counter()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for lec, data in ex.map(one, lectures):
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
                key = (f, t, label)
                if key in seen:
                    dropped["重複"] += 1
                    continue
                seen.add(key)
                page = r["page"] if 1 <= r["page"] <= lec.page_count else 1
                relations.append({"from": f, "to": t, "label": label,
                                  "source": {"lecture": lec.number, "page": page}})
                kept += 1
            print(f"  第{lec.number:2d}回: {len(data['relations']):3d} 件 → 採用 {kept}")
    return relations, dict(dropped)


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


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf_dir", type=Path)
    ap.add_argument("--vocab", type=Path, default=Path("data/vocab.json"))
    ap.add_argument("--out-dir", type=Path, default=Path("data"))
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help="Gemini のモデル名。環境変数 QUIZMAP_MODEL でも指定できる（既定: %(default)s）")
    ap.add_argument("--workers", type=int, default=4, help="関係抽出の並列数")
    ap.add_argument("--dpi", type=int, default=80, help="スライド画像の解像度")
    ap.add_argument("--no-slides", action="store_true", help="画像生成を飛ばす")
    args = ap.parse_args()

    if not args.vocab.exists():
        print(f"[error] {args.vocab} がない。先に derive_vocab.py を実行すること", file=sys.stderr)
        return 1
    vocab = read_json(args.vocab)
    course = vocab.get("course", args.pdf_dir.resolve().name)

    lectures, skipped = load_lectures(args.pdf_dir)
    if not lectures:
        print(f"[error] {args.pdf_dir} に「第N回」の PDF が見つからない", file=sys.stderr)
        return 1
    for p in skipped:
        print(f"[skip] 回番号が読めないので無視: {p.name}", file=sys.stderr)
    total_pages = sum(l.page_count for l in lectures)
    print(f"{course}: {len(lectures)} 回 {total_pages} ページ / model={args.model}")

    write_json(args.out_dir / "pages.json", [
        {"lecture": lec.number, "page": i, "text": page}
        for lec in lectures for i, page in enumerate(lec.pages, start=1)
    ])

    client = make_client()

    print("\n[1/3] 概念の抽出（回ごとに順番に）")
    concepts, unmatched_kind = extract_concepts(client, args.model, course, vocab, lectures)

    print("\n[2/3] 関係の抽出（概念 id を固定して、回ごとに並列）")
    relations, dropped = extract_relations(client, args.model, course, vocab, lectures, concepts, args.workers)

    if args.no_slides:
        n_imgs = 0
    else:
        print("\n[3/3] スライド画像の生成")
        n_imgs = render_slides(lectures, args.out_dir / "slides", args.dpi)
        print(f"  {n_imgs} 枚 → {args.out_dir / 'slides'}")

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
        "model": args.model,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    write_json(args.out_dir / "graph.json", {
        "course": course,
        "concepts": list(concepts.values()),
        "relations": relations,
        "stats": stats,
    })

    print(f"\n{args.out_dir / 'graph.json'} を書いた")
    print(f"概念数:   {len(concepts)}")
    print(f"関係数:   {len(relations)}")
    print(f"relation の「その他」率: {other_rate:.1%} ({n_other}/{len(relations)})"
          + ("  ← 警告: 20% 超。語彙が資料に合っていない可能性（§4）" if other_rate > 0.2 else ""))
    print(f"kind の「その他」率:     {other_kind_rate:.1%} ({n_other_kind}/{len(concepts)})"
          + ("  ← 警告: 20% 超" if other_kind_rate > 0.2 else ""))
    print("relation 別: " + ", ".join(f"{k} {v}" for k, v in label_counts.most_common()))
    print("kind 別:     " + ", ".join(f"{k} {v}" for k, v in kind_counts.most_common()))
    print(f"どの kind にも当てはまらなかった概念: {len(unmatched_kind)} 件"
          + ("（「その他」として保持）" if unmatched_kind else ""))
    for u in unmatched_kind[:5]:
        print(f"  - {u['label']}（モデルが付けた kind: {u['kind_given']!r}、第{u['lecture']}回）")
    if dropped:
        print("関係の除外: " + ", ".join(f"{k} {v}" for k, v in dropped.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
