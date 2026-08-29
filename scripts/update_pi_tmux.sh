#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="${LKT_SOURCE:-/home/lachlan/LocalKnowledgeTerminal/source}"
SESSION_NAME="${LKT_UPDATE_SESSION:-lkt-update}"
LOG_DIR="${LKT_LOG_DIR:-/home/lachlan/LocalKnowledgeTerminal/logs}"
LOG_PATH="$LOG_DIR/update-pi.log"

command -v tmux >/dev/null 2>&1 || {
  echo "tmux is required: sudo apt-get install tmux" >&2
  exit 1
}
[ -x "$SOURCE_DIR/scripts/update_pi.sh" ] || {
  echo "Missing update script at $SOURCE_DIR/scripts/update_pi.sh" >&2
  exit 1
}
if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
  echo "tmux session already exists: $SESSION_NAME" >&2
  echo "Attach with: tmux attach -t $SESSION_NAME" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"
tmux new-session -d -s "$SESSION_NAME" -c "$SOURCE_DIR" \
  "bash -lc './scripts/update_pi.sh 2>&1 | tee \"$LOG_PATH\"'"
tmux set-option -t "$SESSION_NAME" remain-on-exit on

printf 'LKT update started safely in tmux session %s.\n' "$SESSION_NAME"
printf 'Attach: tmux attach -t %s\n' "$SESSION_NAME"
printf 'Log: %s\n' "$LOG_PATH"
