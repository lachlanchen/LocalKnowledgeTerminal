#!/usr/bin/env bash
set -Eeuo pipefail

LKT_HOME="${LKT_HOME:-/home/lachlan/LocalKnowledgeTerminal}"
MODEL_DIR="${LKT_HOME}/models"
MODEL_NAME="Qwen3-8B-Q4_K_M.gguf"
MODEL_PATH="${MODEL_DIR}/${MODEL_NAME}"
PART_PATH="${MODEL_PATH}.partial"
VERIFIED_PATH="${MODEL_PATH}.verified-sha256"
MODEL_URL="https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/${MODEL_NAME}?download=true"
EXPECTED_SHA256="d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785"

mkdir -p "$MODEL_DIR"

if [ -f "$MODEL_PATH" ]; then
  actual_sha256="$(sha256sum "$MODEL_PATH" | cut -d' ' -f1)"
  if [ "$actual_sha256" = "$EXPECTED_SHA256" ]; then
    printf '%s\n' "$EXPECTED_SHA256" >"$VERIFIED_PATH"
    printf '%s is already complete and verified.\n' "$MODEL_NAME"
    exit 0
  fi
  printf 'Refusing to overwrite an existing model with unexpected SHA-256: %s\n' "$MODEL_PATH" >&2
  exit 1
fi

curl --fail --location --retry 8 --retry-delay 5 --continue-at - \
  --output "$PART_PATH" "$MODEL_URL"

printf '%s  %s\n' "$EXPECTED_SHA256" "$PART_PATH" | sha256sum --check --status || {
  printf 'Downloaded file failed SHA-256 verification; partial file retained for inspection.\n' >&2
  exit 1
}

mv "$PART_PATH" "$MODEL_PATH"
printf '%s\n' "$EXPECTED_SHA256" >"$VERIFIED_PATH"
printf 'Verified %s\n' "$MODEL_PATH"
