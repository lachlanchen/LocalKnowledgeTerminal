#!/usr/bin/env bash
set -Eeuo pipefail

LKT_HOME="${LKT_HOME:-/home/lachlan/LocalKnowledgeTerminal}"
MODEL_ENV="/etc/lkt-model.env"
FOUR_B_PATH="${LKT_HOME}/models/Qwen3-4B-Q4_K_M.gguf"
EIGHT_B_PATH="${LKT_HOME}/models/Qwen3-8B-Q4_K_M.gguf"
EIGHT_B_SHA256="d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785"

usage() {
  printf 'Usage: sudo %s 4b|8b|status\n' "$0" >&2
  exit 2
}

status() {
  if [ -r "$MODEL_ENV" ]; then
    grep -E '^(LKT_LLM_MODEL|LKT_MODEL_PATH|LKT_MODEL_CONTEXT)=' "$MODEL_ENV"
  else
    printf 'LKT_LLM_MODEL=Qwen3-4B-Q4_K_M (service default)\n'
  fi
  systemctl is-active lkt-llm.service lkt-web.service
}

write_config() {
  local name="$1" path="$2" context="$3" batch="$4"
  local temporary="${MODEL_ENV}.tmp.$$"
  trap 'rm -f "${temporary:-}"' RETURN
  cat >"$temporary" <<EOF
LKT_LLM_MODEL=${name}
LKT_MODEL_PATH=${path}
LKT_MODEL_CONTEXT=${context}
LKT_BATCH_SIZE=${batch}
LKT_UBATCH_SIZE=64
EOF
  install -o root -g root -m 0644 "$temporary" "$MODEL_ENV"
  rm -f "$temporary"
  trap - RETURN
}

wait_for_model() {
  local attempt
  for attempt in $(seq 1 180); do
    if curl --silent --fail http://127.0.0.1:8081/health >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_for_web() {
  local attempt
  for attempt in $(seq 1 60); do
    if curl --silent --fail http://127.0.0.1:8090/api/health >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

activate() {
  local name="$1" path="$2" context="$3" batch="$4"
  write_config "$name" "$path" "$context" "$batch"
  systemctl daemon-reload
  systemctl restart lkt-llm.service
  wait_for_model
  systemctl restart lkt-web.service
  wait_for_web
}

selection="${1:-}"
[ "$selection" = "status" ] && { status; exit 0; }
[ "$selection" = "4b" ] || [ "$selection" = "8b" ] || usage
[ "${EUID:-$(id -u)}" -eq 0 ] || { printf 'Run with sudo.\n' >&2; exit 1; }
[ -f "$FOUR_B_PATH" ] || { printf 'Missing fallback model: %s\n' "$FOUR_B_PATH" >&2; exit 1; }

if [ "$selection" = "4b" ]; then
  activate "Qwen3-4B-Q4_K_M" "$FOUR_B_PATH" 3072 128
  printf 'Active model: Qwen3-4B Q4_K_M\n'
  exit 0
fi

marker="${EIGHT_B_PATH}.verified-sha256"
[ -f "$EIGHT_B_PATH" ] || { printf 'Missing 8B model; run scripts/download_qwen3_8b.sh first.\n' >&2; exit 1; }
[ -f "$marker" ] && [ "$(cat "$marker")" = "$EIGHT_B_SHA256" ] && [ "$EIGHT_B_PATH" -ot "$marker" ] || {
  printf '8B model is not marked as checksum-verified. Re-run its downloader.\n' >&2
  exit 1
}

if activate "Qwen3-8B-Q4_K_M" "$EIGHT_B_PATH" 2048 64; then
  printf 'Active model: Qwen3-8B Q4_K_M\n'
  exit 0
fi

printf '8B failed its health check; restoring 4B automatically.\n' >&2
activate "Qwen3-4B-Q4_K_M" "$FOUR_B_PATH" 3072 128
exit 1
