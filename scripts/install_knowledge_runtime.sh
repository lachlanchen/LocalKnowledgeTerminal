#!/usr/bin/env bash
set -Eeuo pipefail

LKT_HOME="${LKT_HOME:-/home/lachlan/LocalKnowledgeTerminal}"
SOURCE_DIR="${LKT_HOME}/source"
VENV_DIR="${LKT_HOME}/runtime/knowledge-venv"
WN_DATA_DIR="${LKT_HOME}/data/lexicons/wn"
FREEDICT_DB="${LKT_HOME}/data/lexicons/freedict-eng-ara.sqlite3"
FREEDICT_REV="5bdceeac8d0dba3298c1bebe734f60d54dad30f7"
FREEDICT_SHA256="7572d3685c501975cd0d47b0dfb581b053b28fb18932d06f09d64d0479b06746"
FREEDICT_URL="https://raw.githubusercontent.com/freedict/fd-dictionaries/${FREEDICT_REV}/eng-ara/eng-ara.tei"

[ -f "${SOURCE_DIR}/lkt/graph.py" ] || {
  printf 'Missing LKT checkout at %s\n' "$SOURCE_DIR" >&2
  exit 1
}

if ! command -v espeak-ng >/dev/null 2>&1; then
  DEBIAN_FRONTEND=noninteractive sudo -n apt-get install -y espeak-ng
fi

python3 -m venv "$VENV_DIR"
"${VENV_DIR}/bin/python" -m pip install \
  --disable-pip-version-check \
  --only-binary=:all: \
  "ladybug==0.19.1" \
  "pypinyin==0.55.0" \
  "wn==1.1.1"

install -d -m 0755 "$WN_DATA_DIR"
for lexicon in omw-en:2.0 omw-ja:2.0 omw-cmn:2.0 omw-fr:2.0 omw-arb:2.0; do
  WN_DATA_DIR="$WN_DATA_DIR" \
    "${VENV_DIR}/bin/python" -m wn download "$lexicon"
done

if [ ! -f "$FREEDICT_DB" ]; then
  FREEDICT_SOURCE="$(mktemp --suffix=.tei)"
  cleanup_freedict() { rm -f -- "$FREEDICT_SOURCE"; }
  trap cleanup_freedict EXIT
  curl --fail --location --retry 3 --output "$FREEDICT_SOURCE" "$FREEDICT_URL"
  printf '%s  %s\n' "$FREEDICT_SHA256" "$FREEDICT_SOURCE" | sha256sum --check --status
  env \
    PYTHONPATH="$SOURCE_DIR" \
    LKT_SOURCE="$SOURCE_DIR" \
    LKT_DATA_DIR="${LKT_HOME}/data" \
    LKT_FREEDICT_DB="$FREEDICT_DB" \
    "${VENV_DIR}/bin/python" -m lkt.cli ingest-freedict "$FREEDICT_SOURCE"
  cleanup_freedict
  trap - EXIT
fi

env \
  PYTHONPATH="$SOURCE_DIR" \
  LKT_SOURCE="$SOURCE_DIR" \
  LKT_DATA_DIR="${LKT_HOME}/data" \
  LKT_FREEDICT_DB="$FREEDICT_DB" \
  WN_DATA_DIR="$WN_DATA_DIR" \
  "${VENV_DIR}/bin/python" -m lkt.cli knowledge-status >/dev/null

printf 'Knowledge runtime ready: %s\n' "$VENV_DIR"
printf 'WordNet data ready: %s\n' "$WN_DATA_DIR"
printf 'FreeDict English-Arabic index ready: %s\n' "$FREEDICT_DB"
