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
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quizmap.common import DEFAULT_MODEL, MissingAPIKeyError, load_lectures, make_client, read_json  # noqa: E402
from quizmap.extraction import run_extraction  # noqa: E402


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
    lectures, skipped = load_lectures(args.pdf_dir)
    if not lectures:
        print(f"[error] {args.pdf_dir} に回番号の読める PDF が見つからない", file=sys.stderr)
        return 1
    for p in skipped:
        print(f"[skip] 回番号が読めないので無視: {p.name}", file=sys.stderr)
    graph = run_extraction(make_client(), args.model, read_json(args.vocab), lectures, args.out_dir,
                           workers=args.workers, dpi=args.dpi, slides=not args.no_slides)
    print(json.dumps(graph["stats"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except MissingAPIKeyError as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)
