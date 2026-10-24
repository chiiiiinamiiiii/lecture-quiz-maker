#!/usr/bin/env python
"""使える Gemini モデル名を表示する（--model に何を渡せるかの確認用）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quizmap.common import MissingAPIKeyError, list_models, make_client  # noqa: E402

try:
    client = make_client()
except MissingAPIKeyError as e:
    print(f"[error] {e}", file=sys.stderr)
    sys.exit(1)

for m in list_models(client):
    print(f"{m['name']:45s} in={m['inputTokenLimit']!s:>8} out={m['outputTokenLimit']!s:>6}  {m['displayName']}")
