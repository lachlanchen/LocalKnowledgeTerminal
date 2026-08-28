#!/usr/bin/env bash
set -Eeuo pipefail

[ "${EUID:-$(id -u)}" -eq 0 ] || { echo "Run with sudo" >&2; exit 1; }

LKT_USER="${LKT_USER:-lachlan}"
LKT_HOME="/home/${LKT_USER}/LocalKnowledgeTerminal"
SOURCE_DIR="${LKT_HOME}/source"
CORPUS_SOURCE="${1:-}"
MODEL_PATH="${LKT_HOME}/models/Qwen3-4B-Q4_K_M.gguf"
LLAMA_SERVER="${LKT_HOME}/runtime/llama.cpp/build/bin/llama-server"

[ -f "$SOURCE_DIR/lkt/web.py" ] || { echo "Missing Git checkout at $SOURCE_DIR" >&2; exit 1; }
[ -x "$LLAMA_SERVER" ] || { echo "Missing llama-server; run scripts/bootstrap_runtime.sh first" >&2; exit 1; }
[ -f "$MODEL_PATH" ] || { echo "Missing Qwen model; run scripts/bootstrap_runtime.sh first" >&2; exit 1; }
[ -f "$CORPUS_SOURCE" ] || { echo "Usage: sudo $0 /path/to/entries.jsonl" >&2; exit 1; }

install -d -o "$LKT_USER" -g "$LKT_USER" -m 0755 "$LKT_HOME/data" "$LKT_HOME/logs"

cat >/etc/lkt.env <<EOF
LKT_SOURCE=${SOURCE_DIR}
LKT_DATA_DIR=${LKT_HOME}/data
LKT_CORPUS_DB=${LKT_HOME}/data/word-origins.sqlite3
LKT_CARDS_DB=${LKT_HOME}/data/cards.sqlite3
LKT_LLM_URL=http://127.0.0.1:8081/v1/chat/completions
LKT_LLM_MODEL=Qwen3-4B-Q4_K_M
LKT_REQUEST_TIMEOUT=240
LKT_MAX_EVIDENCE=4
LKT_HOST=0.0.0.0
LKT_PORT=8090
EOF
chmod 0644 /etc/lkt.env

runuser -u "$LKT_USER" -- env \
  PYTHONPATH="$SOURCE_DIR" \
  LKT_SOURCE="$SOURCE_DIR" \
  LKT_DATA_DIR="$LKT_HOME/data" \
  python3 -m lkt.cli ingest "$CORPUS_SOURCE"

install -o root -g root -m 0644 "$SOURCE_DIR/systemd/lkt-llm.service" /etc/systemd/system/
install -o root -g root -m 0644 "$SOURCE_DIR/systemd/lkt-web.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now lkt-llm.service

for attempt in $(seq 1 120); do
  if curl --silent --fail http://127.0.0.1:8081/health >/dev/null; then break; fi
  if [ "$attempt" -eq 120 ]; then
    echo "Model server did not become ready" >&2
    journalctl -u lkt-llm.service -n 60 --no-pager >&2
    exit 1
  fi
  sleep 2
done

systemctl enable --now lkt-web.service
printf 'LKT installed. Open http://127.0.0.1:8090\n'
