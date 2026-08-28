#!/usr/bin/env bash
set -Eeuo pipefail

LKT_HOME="${LKT_HOME:-/home/lachlan/LocalKnowledgeTerminal}"
SOURCE_DIR="${LKT_HOME}/source"
LOG_DIR="${LKT_HOME}/logs"
RESULT_PATH="${1:-${LOG_DIR}/model-benchmark-$(date -u +%Y%m%dT%H%M%SZ).jsonl}"

[ "${EUID:-$(id -u)}" -eq 0 ] || {
  printf 'Run with sudo so the benchmark can switch models safely.\n' >&2
  exit 1
}

mkdir -p "$LOG_DIR"
touch "$RESULT_PATH"
chmod 0644 "$RESULT_PATH"

ORIGINAL_PROFILE="4b"
if [ -r /etc/lkt-model.env ] && grep -q '^LKT_LLM_MODEL=Qwen3-8B' /etc/lkt-model.env; then
  ORIGINAL_PROFILE="8b"
fi

restore_original() {
  "$SOURCE_DIR/scripts/select_model.sh" "$ORIGINAL_PROFILE" >/dev/null 2>&1 || true
}
trap restore_original EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for profile in 4b 8b; do
  printf 'Activating %s for one bounded benchmark...\n' "$profile"
  "$SOURCE_DIR/scripts/select_model.sh" "$profile"
  python3 "$SOURCE_DIR/scripts/benchmark_model.py" | tee -a "$RESULT_PATH"
done

restore_original
trap - EXIT INT TERM
printf 'Benchmarks complete; %s restored. Results: %s\n' "$ORIGINAL_PROFILE" "$RESULT_PATH"
