#!/usr/bin/env bash
set -Eeuo pipefail

LKT_HOME="${LKT_HOME:-/home/lachlan/LocalKnowledgeTerminal}"
SOURCE_DIR="${LKT_SOURCE:-${LKT_HOME}/source}"
VENV_DIR="${LKT_HOME}/runtime/knowledge-venv"
JMDICT_DB="${LKT_JMDICT_DB:-${LKT_HOME}/data/lexicons/jmdict.sqlite3}"
JMDICT_RELEASE="3.6.2+20260824122934"
JMDICT_FILE="jmdict-eng-3.6.2+20260824122934.json.tgz"
JMDICT_SHA256="d9b74539bce7df82491a57ad96a0634a988129db6ca4a362f7221bc5e736871f"
JMDICT_URL="https://github.com/scriptin/jmdict-simplified/releases/download/3.6.2%2B20260824122934/jmdict-eng-3.6.2%2B20260824122934.json.tgz"

[ -f "${SOURCE_DIR}/lkt/jmdict.py" ] || {
  printf 'Missing LKT JMdict indexer at %s\n' "$SOURCE_DIR" >&2
  exit 1
}
[ -x "${VENV_DIR}/bin/python" ] || {
  printf 'Missing knowledge runtime at %s\n' "$VENV_DIR" >&2
  exit 1
}

if [ -f "$JMDICT_DB" ] && env \
  PYTHONPATH="$SOURCE_DIR" LKT_SOURCE="$SOURCE_DIR" \
  "${VENV_DIR}/bin/python" -c \
  "from pathlib import Path; from lkt.jmdict import JapaneseReadingIndex; raise SystemExit(JapaneseReadingIndex(Path('$JMDICT_DB')).metadata().get('release') != '$JMDICT_RELEASE')"; then
  printf 'JMdict index already current: %s\n' "$JMDICT_DB"
  exit 0
fi

TEMP_DIR="$(mktemp -d)"
cleanup() {
  if [ -n "${TEMP_DIR:-}" ] && [ "$TEMP_DIR" != "/" ] && [ -d "$TEMP_DIR" ]; then
    rm -rf -- "$TEMP_DIR"
  fi
}
trap cleanup EXIT

ARCHIVE="${TEMP_DIR}/${JMDICT_FILE}"
curl --fail --location --retry 3 --output "$ARCHIVE" "$JMDICT_URL"
printf '%s  %s\n' "$JMDICT_SHA256" "$ARCHIVE" | sha256sum --check --status
tar -xzf "$ARCHIVE" -C "$TEMP_DIR"
JSON_SOURCE="${TEMP_DIR}/jmdict-eng-3.6.2.json"
[ -f "$JSON_SOURCE" ] || { printf 'JMdict JSON was not extracted\n' >&2; exit 1; }

install -d -m 0755 "$(dirname "$JMDICT_DB")"
env \
  PYTHONPATH="$SOURCE_DIR" \
  LKT_SOURCE="$SOURCE_DIR" \
  LKT_DATA_DIR="${LKT_HOME}/data" \
  LKT_JMDICT_DB="$JMDICT_DB" \
  "${VENV_DIR}/bin/python" -m lkt.cli ingest-jmdict "$JSON_SOURCE" \
  --release "$JMDICT_RELEASE"

printf 'JMdict reading index ready: %s\n' "$JMDICT_DB"
