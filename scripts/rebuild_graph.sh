#!/usr/bin/env bash
set -Eeuo pipefail

LKT_HOME="${LKT_HOME:-/home/lachlan/LocalKnowledgeTerminal}"
SOURCE_DIR="${LKT_HOME}/source"
PYTHON="${LKT_HOME}/runtime/knowledge-venv/bin/python"

[ -x "$PYTHON" ] || {
  printf 'Knowledge runtime missing; run scripts/install_knowledge_runtime.sh first.\n' >&2
  exit 1
}

exec env \
  PYTHONPATH="$SOURCE_DIR" \
  LKT_SOURCE="$SOURCE_DIR" \
  LKT_DATA_DIR="${LKT_HOME}/data" \
  "$PYTHON" -m lkt.cli rebuild-graph --replace "$@"
