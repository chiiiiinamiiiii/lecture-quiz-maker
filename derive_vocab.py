#!/usr/bin/env python
"""§4 語彙の導出。PDF フォルダを受け取り vocab.json を出力する。

使い方:
    .venv/bin/python derive_vocab.py <pdf_dir> [--out data/vocab.json] [--model gemini-3.1-flash-lite]

既に vocab.json があれば何もしない（作り直したいときはファイルを消す）。
人間の確認工程は挟まない（全自動）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quizmap.common import DEFAULT_MODEL, MissingAPIKeyError, load_lectures, make_client, write_json  # noqa: E402
from quizmap.vocab import derive_vocab  # noqa: E402


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
        print(f"[error] {args.pdf_dir} に回番号の読める PDF が見つからない", file=sys.stderr)
        return 1
    for p in skipped:
        print(f"[skip] 回番号が読めないので無視: {p.name}", file=sys.stderr)
    vocab = derive_vocab(make_client(), args.model, lectures, args.pdf_dir.resolve().name)
    vocab["meta"]["pdf_dir"] = str(args.pdf_dir.resolve())
    write_json(args.out, vocab)
    print(f"{args.out} を書いた（course: {vocab['course']}）")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except MissingAPIKeyError as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)
