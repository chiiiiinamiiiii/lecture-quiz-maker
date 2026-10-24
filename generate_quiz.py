#!/usr/bin/env python
"""§7 クイズ生成の確認用 CLI。

使い方:
    .venv/bin/python generate_quiz.py --lecture 3 --n 5          # 第3回の概念を出題範囲にする
    .venv/bin/python generate_quiz.py --ids kant,gimu_ron --n 5  # id を直接指定
出力: data/quiz_latest.json と標準出力（JSON）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quizmap.common import DEFAULT_MODEL, MissingAPIKeyError, make_client, read_json, write_json  # noqa: E402
from quizmap.quiz import generate_quiz  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", help="カンマ区切りの概念 id")
    ap.add_argument("--lecture", type=int, help="この回に登場する概念を出題範囲にする")
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()

    graph = read_json(DATA / "graph.json")
    vocab = read_json(DATA / "vocab.json")
    pages = read_json(DATA / "pages.json")
    if args.ids:
        ids = [s.strip() for s in args.ids.split(",") if s.strip()]
    elif args.lecture:
        ids = [c["id"] for c in graph["concepts"] if args.lecture in c["lectures"]]
    else:
        ap.error("--ids か --lecture を指定すること")
    print(f"出題範囲: {len(ids)} 概念 / {args.n} 問 / model={args.model}", file=sys.stderr)

    result = generate_quiz(make_client(), args.model, graph, vocab, pages, ids, args.n)
    write_json(DATA / "quiz_latest.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    for w in result["warnings"]:
        print("[warn]", w, file=sys.stderr)
    m = result["meta"]
    print(f"\n所要時間 {m['seconds']} 秒 / API 呼び出し {m['api_calls']} 回 / 誤答差し替え {m['distractors_replaced']} 件",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except MissingAPIKeyError as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)
