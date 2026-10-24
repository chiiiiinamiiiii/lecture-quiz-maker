#!/usr/bin/env bash
# 本体の .venv の Python を、この作業ツリーの役割の環境（データ置き場・API キーの場所）で動かす。
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
source dev/env.sh
exec "$PY" "$@"
