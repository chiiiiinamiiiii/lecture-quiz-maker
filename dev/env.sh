# 役割ごとの作業環境を決める（source して使う）。作業ツリーのブランチ名 team/<役割> から役割を読む。
MAIN="$(cd "$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")" && pwd)"
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
ROLE="${QUIZMAP_ROLE:-${BRANCH#team/}}"
[ "$ROLE" = "$BRANCH" ] && ROLE="main"
case "$ROLE" in
  main)            PORT=8501 ;;
  ui-reviewer)     PORT=8611 ;;
  ui-improver)     PORT=8612 ;;
  output-reviewer) PORT=8613 ;;
  prompt-improver) PORT=8614 ;;
  feature-dev)     PORT=8615 ;;
  *)               PORT=8699 ;;
esac
if [ "$ROLE" = "main" ]; then DATA="$MAIN/data"; else DATA="$MAIN/data/team/$ROLE"; fi
export QUIZMAP_ROLE="$ROLE" QUIZMAP_PORT="$PORT"
export QUIZMAP_DATA_DIR="$DATA" QUIZMAP_REAL_DATA_DIR="$DATA"
export QUIZMAP_ENV_FILE="$MAIN/.env"
PY="$MAIN/.venv/bin/python"
