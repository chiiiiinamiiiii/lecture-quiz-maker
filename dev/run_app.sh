#!/usr/bin/env bash
# この作業ツリーのアプリを、役割ごとのポートとデータ置き場で起動する。
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
source dev/env.sh
echo "role=$QUIZMAP_ROLE  http://localhost:$QUIZMAP_PORT/  data=$QUIZMAP_DATA_DIR"
exec "$PY" -m streamlit run app.py --server.port "$QUIZMAP_PORT" --server.headless true --server.runOnSave true \
  --browser.gatherUsageStats false
