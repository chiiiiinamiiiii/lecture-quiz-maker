#!/usr/bin/env python
"""§4 語彙の導出。PDF フォルダを受け取り vocab.json を出力する。

使い方:
    .venv/bin/python derive_vocab.py <pdf_dir> [--out data/vocab.json] [--model gemini-3.1-flash-lite]

既に vocab.json があれば何もしない（作り直したいときはファイルを消す）。
人間の確認工程は挟まない（全自動）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quizmap.common import (  # noqa: E402
    DEFAULT_MODEL, OTHER_LABEL, call_json, lecture_as_prompt_text, load_lectures,
    make_client, write_json,
)

# 1 回の呼び出しに入れる文字数の上限。超えたら代表的な回を等間隔に選ぶ（§4「または代表的な数回分」）
MAX_CORPUS_CHARS = 150_000

SYSTEM = """あなたは大学の授業資料を読み、その授業に固有の「概念の種類」と「概念どうしの関係ラベル」を設計する専門家です。
出力は日本語で書きます。資料に実際に現れるものだけを挙げ、一般論で思いつくものを足してはいけません。"""

USER_TEMPLATE = """以下は「{course_hint}」という授業の資料です（{n_lectures}回分、ページ番号付き）。

この資料に出てくる概念どうしの関係を分類するラベルを 6〜12 個、提案してください。
各ラベルについて、
 (a) 名前（短い日本語。例: 提唱、対立、批判、前提）
 (b) 定義（どういうときにこのラベルを付けるか）
 (c) その関係をクイズにするときの問い方（例: 「AとBの違いを問う」「誰がAを提唱したかを問う」）
 (d) 資料中の実例（「A ⟷ B」のように概念名で）
を書いてください。

守ること:
- 資料に実際に現れる関係だけを挙げること。一般論で思いつくラベルを足さないこと
- 出現回数の多寡で絞らないこと。1 度しか出てこない関係でも、資料にあるなら挙げてよい
- 「{other}」というラベルは作らないこと（こちらで別途用意します）

同様に、概念の「種類」も 4〜8 個提案してください。各種類について、名前・定義・資料中の実例を書いてください。

最後に、この授業の名前を資料から読み取って course に入れてください。

=== 資料 ===
{corpus}
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "course": {"type": "string"},
        "kinds": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "definition": {"type": "string"},
                    "example": {"type": "string"},
                },
                "required": ["name", "definition", "example"],
                "additionalProperties": False,
            },
        },
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "definition": {"type": "string"},
                    "question_style": {"type": "string"},
                    "example": {"type": "string"},
                },
                "required": ["label", "definition", "question_style", "example"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["course", "kinds", "relations"],
    "additionalProperties": False,
}


def choose_lectures(lectures, max_chars: int):
    """全体が上限に収まればそのまま。収まらなければ等間隔に代表的な回を選ぶ。"""
    sizes = [sum(len(p) for p in lec.pages) for lec in lectures]
    if sum(sizes) <= max_chars:
        return lectures
    avg = sum(sizes) / len(sizes)
    k = max(2, int(max_chars // avg))
    step = len(lectures) / k
    idx = sorted({int(i * step) for i in range(k)})
    return [lectures[i] for i in idx]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf_dir", type=Path)
    ap.add_argument("--out", type=Path, default=Path("data/vocab.json"))
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help="Gemini のモデル名。環境変数 QUIZMAP_MODEL でも指定できる（既定: %(default)s）")
    args = ap.parse_args()

    if args.out.exists():
        print(f"{args.out} は既にある。何もしない（作り直すならファイルを消す）")
        return 0

    lectures, skipped = load_lectures(args.pdf_dir)
    if not lectures:
        print(f"[error] {args.pdf_dir} に「第N回」の PDF が見つからない", file=sys.stderr)
        return 1
    for p in skipped:
        print(f"[skip] 回番号が読めないので無視: {p.name}", file=sys.stderr)

    chosen = choose_lectures(lectures, MAX_CORPUS_CHARS)
    if len(chosen) < len(lectures):
        print(f"[info] 資料が大きいので代表 {len(chosen)} 回を使う: {[l.number for l in chosen]}")
    corpus = "\n\n".join(lecture_as_prompt_text(lec) for lec in chosen)
    course_hint = args.pdf_dir.resolve().name

    print(f"{len(chosen)} 回分 {len(corpus):,} 文字を {args.model} に渡して語彙を導出中…")
    client = make_client()
    vocab = call_json(
        client, model=args.model, system=SYSTEM, schema=SCHEMA,
        user=USER_TEMPLATE.format(
            course_hint=course_hint, n_lectures=len(chosen), other=OTHER_LABEL, corpus=corpus,
        ),
        max_tokens=16000,
    )

    # 「その他」を常に 1 つ用意する（§3）。LLM が作っていたら除いて付け直す
    vocab["relations"] = [r for r in vocab["relations"] if r["label"] != OTHER_LABEL]
    vocab["relations"].append({
        "label": OTHER_LABEL,
        "definition": "上のどのラベルにも当てはまらない関係",
        "question_style": "出題しない",
        "example": "",
    })
    vocab["meta"] = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "model": args.model,
        "pdf_dir": str(args.pdf_dir.resolve()),
        "lectures_used": [lec.number for lec in chosen],
    }
    write_json(args.out, vocab)

    print(f"\n{args.out} を書いた")
    print(f"course: {vocab['course']}")
    print(f"kinds ({len(vocab['kinds'])}): " + " / ".join(k["name"] for k in vocab["kinds"]))
    print(f"relations ({len(vocab['relations'])}): " + " / ".join(r["label"] for r in vocab["relations"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
