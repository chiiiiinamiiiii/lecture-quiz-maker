#!/usr/bin/env bash
# 開発チームの作業環境を用意し、役割ごとに Terminal を開いて Claude Code を起動する。
#
#   dev/team.sh setup            役割ごとの作業ツリー・ブランチ・データ置き場を作る（何度実行してもよい）
#   dev/team.sh open [役割...]   Terminal のウィンドウを開き、その役割として Claude Code を起動する（省略時は全員）
#   dev/team.sh start <役割>     （open から呼ばれる）この端末でその役割として Claude Code を起動する
#   dev/team.sh status           作業ツリーとブランチの一覧
set -euo pipefail
ROLES=(ui-reviewer ui-improver output-reviewer prompt-improver feature-dev)
MAIN="$(cd "$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")" && pwd)"
TEAM_DIR="$(dirname "$MAIN")/quizmap-team"

fixture_course() {   # 取り込み済みで最も新しいコース（各役割のデータ置き場の初期値にする）
  ls -dt "$MAIN"/data/courses/*/ 2>/dev/null | while read -r d; do [ -f "$d/graph.json" ] && { echo "${d%/}"; break; }; done
}

setup() {
  mkdir -p "$TEAM_DIR" "$MAIN/data/review"
  local src; src="$(fixture_course)"
  for r in "${ROLES[@]}"; do
    git -C "$MAIN" show-ref --quiet "refs/heads/team/$r" || git -C "$MAIN" branch "team/$r" main
    [ -d "$TEAM_DIR/$r" ] || git -C "$MAIN" worktree add "$TEAM_DIR/$r" "team/$r" >/dev/null
    local data="$MAIN/data/team/$r"
    if [ ! -d "$data/courses" ] && [ -n "$src" ]; then
      mkdir -p "$data/courses"
      rsync -a --exclude results.json --exclude job.json --exclude quiz_latest.json "$src" "$data/courses/"
    fi
    echo "$r: $TEAM_DIR/$r  (branch team/$r, data $data)"
  done
}

start() {
  local r="$1"; shift || true
  cd "$TEAM_DIR/$r"
  # 指揮のセッションから Terminal を起動すると、その環境変数（セッション ID・メッセージ用の接続情報など）が
  # 引き継がれ、各役割が「指揮の子セッション」扱いになる。独立したセッションにするため消してから起動する
  local v
  for v in $(env | cut -d= -f1 | grep -E '^(CLAUDE|CLAUDECODE|MCP_CONNECTION)'); do unset "$v"; done
  local args=(--agent "$r" --name "$r" --permission-mode auto)
  [ "$r" = "ui-reviewer" ] && args+=(--chrome)
  exec claude "${args[@]}" "$@"
}

open_terminals() {
  local roles=("$@"); [ ${#roles[@]} -eq 0 ] && roles=("${ROLES[@]}")
  for r in "${roles[@]}"; do
    local first="CLAUDE.md と、あなたの役割の定義（.claude/agents/$r.md）を読んでください。作業ツリー・ブランチ・ポート（dev/env.sh）・データ置き場を確認したら、3 行以内で報告し、指揮からの指示を待ってください。まだ作業は始めないでください。"
    local cmd="cd '$TEAM_DIR/$r' && printf '\\\\e]0;$r\\\\a' && '$MAIN/dev/team.sh' start '$r' '$first'"
    osascript -e "tell application \"Terminal\" to do script \"$cmd\"" >/dev/null
    echo "opened: $r"
  done
  osascript -e 'tell application "Terminal" to activate' >/dev/null
}

case "${1:-}" in
  setup)  setup ;;
  open)   shift; open_terminals "$@" ;;
  start)  shift; start "$@" ;;
  status) git -C "$MAIN" worktree list ;;
  *) sed -n '2,8p' "$0"; exit 1 ;;
esac
