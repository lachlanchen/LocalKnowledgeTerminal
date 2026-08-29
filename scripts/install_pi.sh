#!/usr/bin/env bash
set -Eeuo pipefail

[ "${EUID:-$(id -u)}" -eq 0 ] || { echo "Run with sudo" >&2; exit 1; }

LKT_USER="${LKT_USER:-lachlan}"
LKT_HOME="/home/${LKT_USER}/LocalKnowledgeTerminal"
SOURCE_DIR="${LKT_HOME}/source"
CORPUS_SOURCE="${1:-}"
ANSWERS_SOURCE="${2:-}"
QUESTIONS_SOURCE="${3:-}"
ROOTS_SOURCE="${4:-}"
AFFIXES_SOURCE="${5:-}"
MODEL_PATH="${LKT_HOME}/models/Qwen3-4B-Q4_K_M.gguf"
LLAMA_SERVER="${LKT_HOME}/runtime/llama.cpp-0.3.0/build/bin/llama-server"
KNOWLEDGE_PYTHON="${LKT_HOME}/runtime/knowledge-venv/bin/python"

[ -f "$SOURCE_DIR/lkt/web.py" ] || { echo "Missing Git checkout at $SOURCE_DIR" >&2; exit 1; }
[ -x "$LLAMA_SERVER" ] || { echo "Missing llama-server; run scripts/bootstrap_runtime.sh first" >&2; exit 1; }
[ -f "$MODEL_PATH" ] || { echo "Missing Qwen model; run scripts/bootstrap_runtime.sh first" >&2; exit 1; }
[ -x "$KNOWLEDGE_PYTHON" ] || { echo "Missing knowledge runtime; run scripts/install_knowledge_runtime.sh first" >&2; exit 1; }
[ -f "$CORPUS_SOURCE" ] || {
  echo "Usage: sudo $0 /path/to/entries.jsonl [/path/to/answers.jsonl /path/to/questions.jsonl /path/to/roots.jsonl /path/to/affixes.jsonl]" >&2
  exit 1
}
[ -z "$ANSWERS_SOURCE" ] || [ -f "$ANSWERS_SOURCE" ] || { echo "Missing Answers JSONL: $ANSWERS_SOURCE" >&2; exit 1; }
[ -z "$QUESTIONS_SOURCE" ] || [ -f "$QUESTIONS_SOURCE" ] || { echo "Missing Questions JSONL: $QUESTIONS_SOURCE" >&2; exit 1; }
[ -z "$ROOTS_SOURCE" ] || [ -f "$ROOTS_SOURCE" ] || { echo "Missing Root JSONL: $ROOTS_SOURCE" >&2; exit 1; }
[ -z "$AFFIXES_SOURCE" ] || [ -f "$AFFIXES_SOURCE" ] || { echo "Missing Affix JSONL: $AFFIXES_SOURCE" >&2; exit 1; }

install -d -o "$LKT_USER" -g "$LKT_USER" -m 0755 "$LKT_HOME/data" "$LKT_HOME/logs"

python3 -m pip install --break-system-packages --disable-pip-version-check \
  "pypinyin==0.55.0"

cat >/etc/lkt.env <<EOF
LKT_SOURCE=${SOURCE_DIR}
LKT_DATA_DIR=${LKT_HOME}/data
LKT_CORPUS_DB=${LKT_HOME}/data/word-origins.sqlite3
LKT_ANSWERS_DB=${LKT_HOME}/data/book-of-answers.sqlite3
LKT_QUESTIONS_DB=${LKT_HOME}/data/book-of-questions.sqlite3
LKT_ROOTS_DB=${LKT_HOME}/data/english-roots.sqlite3
LKT_AFFIXES_DB=${LKT_HOME}/data/english-affixes.sqlite3
LKT_CARDS_DB=${LKT_HOME}/data/cards.sqlite3
LKT_KNOWLEDGE_DB=${LKT_HOME}/data/knowledge.sqlite3
LKT_GRAPH_DB=${LKT_HOME}/data/knowledge-graph.lbdb
LKT_FREEDICT_DB=${LKT_HOME}/data/lexicons/freedict-eng-ara.sqlite3
LKT_LLM_URL=http://127.0.0.1:8081/v1/chat/completions
LKT_LLM_MODEL=Qwen3-4B-Q4_K_M
LKT_REQUEST_TIMEOUT=720
LKT_MAX_EVIDENCE=4
LKT_HOST=0.0.0.0
LKT_PORT=8090
EOF
chmod 0644 /etc/lkt.env

if [ ! -f /etc/lkt-model.env ]; then
  cat >/etc/lkt-model.env <<EOF
LKT_LLM_MODEL=Qwen3-4B-Q4_K_M
LKT_MODEL_PATH=${MODEL_PATH}
LKT_MODEL_CONTEXT=3072
LKT_BATCH_SIZE=128
LKT_UBATCH_SIZE=64
EOF
  chmod 0644 /etc/lkt-model.env
fi

runuser -u "$LKT_USER" -- env \
  PYTHONPATH="$SOURCE_DIR" \
  LKT_SOURCE="$SOURCE_DIR" \
  LKT_DATA_DIR="$LKT_HOME/data" \
  python3 -m lkt.cli ingest "$CORPUS_SOURCE"

if [ -n "$ANSWERS_SOURCE" ]; then
  runuser -u "$LKT_USER" -- env \
    PYTHONPATH="$SOURCE_DIR" LKT_SOURCE="$SOURCE_DIR" LKT_DATA_DIR="$LKT_HOME/data" \
    python3 -m lkt.cli ingest-card-book answer "$ANSWERS_SOURCE"
fi

if [ -n "$QUESTIONS_SOURCE" ]; then
  runuser -u "$LKT_USER" -- env \
    PYTHONPATH="$SOURCE_DIR" LKT_SOURCE="$SOURCE_DIR" LKT_DATA_DIR="$LKT_HOME/data" \
    python3 -m lkt.cli ingest-card-book question "$QUESTIONS_SOURCE"
fi

if [ -n "$ROOTS_SOURCE" ]; then
  runuser -u "$LKT_USER" -- env \
    PYTHONPATH="$SOURCE_DIR" LKT_SOURCE="$SOURCE_DIR" LKT_DATA_DIR="$LKT_HOME/data" \
    python3 -m lkt.cli ingest-morphology root "$ROOTS_SOURCE"
fi

if [ -n "$AFFIXES_SOURCE" ]; then
  runuser -u "$LKT_USER" -- env \
    PYTHONPATH="$SOURCE_DIR" LKT_SOURCE="$SOURCE_DIR" LKT_DATA_DIR="$LKT_HOME/data" \
    python3 -m lkt.cli ingest-morphology affix "$AFFIXES_SOURCE"
fi

runuser -u "$LKT_USER" -- env \
  PYTHONPATH="$SOURCE_DIR" LKT_SOURCE="$SOURCE_DIR" LKT_DATA_DIR="$LKT_HOME/data" \
  python3 -m lkt.cli knowledge-status >/dev/null

LKT_USER="$LKT_USER" LKT_SOURCE="$SOURCE_DIR" \
  "$SOURCE_DIR/scripts/install_services.sh" --start
printf 'LKT installed. The next desktop login opens http://127.0.0.1:8090/?display\n'
