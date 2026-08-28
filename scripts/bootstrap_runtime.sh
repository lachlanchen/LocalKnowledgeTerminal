#!/usr/bin/env bash
set -Eeuo pipefail

LKT_HOME="${LKT_HOME:-/home/lachlan/LocalKnowledgeTerminal}"
LLAMA_TAG="${LLAMA_TAG:-v0.3.0}"
LLAMA_COMMIT="c1d0e7a004015f23bc0233470b747b596f29b264"
MODEL_REPO="Qwen/Qwen3-4B-GGUF"
MODEL_REV="bc640142c66e1fdd12af0bd68f40445458f3869b"
MODEL_FILE="Qwen3-4B-Q4_K_M.gguf"
MODEL_SHA256="7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5"
MODEL_URL="https://huggingface.co/${MODEL_REPO}/resolve/${MODEL_REV}/${MODEL_FILE}?download=true"
RUNTIME_DIR="${LKT_HOME}/runtime/llama.cpp-0.3.0"
SOURCE_ARCHIVE="${LKT_HOME}/runtime/llama.cpp-${LLAMA_COMMIT}.tar.gz"
SOURCE_URL="https://github.com/ggml-org/llama.cpp/archive/${LLAMA_COMMIT}.tar.gz"
MODEL_DIR="${LKT_HOME}/models"

for command_name in tar cmake gcc g++ curl sha256sum; do
  command -v "$command_name" >/dev/null 2>&1 || {
    printf 'Missing %s. On Debian install: cmake build-essential git curl libopenblas-dev\n' "$command_name" >&2
    exit 1
  }
done

mkdir -p "$LKT_HOME" "$MODEL_DIR" "${LKT_HOME}/logs"

if [ ! -f "$RUNTIME_DIR/.lkt-source-commit" ]; then
  [ ! -e "$RUNTIME_DIR" ] || {
    echo "Unverified runtime directory already exists: $RUNTIME_DIR" >&2
    exit 1
  }
  curl --fail --location --retry 10 --retry-delay 5 --continue-at - \
    --output "$SOURCE_ARCHIVE" "$SOURCE_URL"
  mkdir -p "$RUNTIME_DIR"
  tar -xzf "$SOURCE_ARCHIVE" --strip-components=1 -C "$RUNTIME_DIR"
  printf '%s\n' "$LLAMA_COMMIT" >"$RUNTIME_DIR/.lkt-source-commit"
fi

[ "$(cat "$RUNTIME_DIR/.lkt-source-commit")" = "$LLAMA_COMMIT" ] || {
  echo "llama.cpp source revision mismatch" >&2
  exit 1
}

cmake -S "$RUNTIME_DIR" -B "$RUNTIME_DIR/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_NATIVE=ON \
  -DGGML_BLAS=ON \
  -DGGML_BLAS_VENDOR=OpenBLAS \
  -DLLAMA_CURL=OFF \
  -DLLAMA_BUILD_TESTS=OFF
cmake --build "$RUNTIME_DIR/build" --config Release -j "$(nproc)" \
  --target llama-cli llama-server

MODEL_PATH="${MODEL_DIR}/${MODEL_FILE}"
if [ -f "$MODEL_PATH" ] && printf '%s  %s\n' "$MODEL_SHA256" "$MODEL_PATH" | sha256sum --check --status; then
  printf 'Model already verified: %s\n' "$MODEL_PATH"
else
  curl --fail --location --retry 10 --retry-delay 5 --continue-at - \
    --output "$MODEL_PATH" "$MODEL_URL"
  printf '%s  %s\n' "$MODEL_SHA256" "$MODEL_PATH" | sha256sum --check
fi

printf 'Runtime ready\nllama.cpp: %s\nmodel: %s\n' "$LLAMA_TAG" "$MODEL_PATH"
