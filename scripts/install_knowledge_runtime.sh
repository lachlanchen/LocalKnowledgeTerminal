#!/usr/bin/env bash
set -Eeuo pipefail

LKT_HOME="${LKT_HOME:-/home/lachlan/LocalKnowledgeTerminal}"
SOURCE_DIR="${LKT_HOME}/source"
VENV_DIR="${LKT_HOME}/runtime/knowledge-venv"
WN_DATA_DIR="${LKT_HOME}/data/lexicons/wn"

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
  "wn==1.1.1"

install -d -m 0755 "$WN_DATA_DIR"
for lexicon in omw-en:2.0 omw-ja:2.0 omw-cmn:2.0 omw-fr:2.0 omw-arb:2.0; do
  WN_DATA_DIR="$WN_DATA_DIR" \
    "${VENV_DIR}/bin/python" -m wn download "$lexicon"
done

env \
  PYTHONPATH="$SOURCE_DIR" \
  LKT_SOURCE="$SOURCE_DIR" \
  LKT_DATA_DIR="${LKT_HOME}/data" \
  WN_DATA_DIR="$WN_DATA_DIR" \
  "${VENV_DIR}/bin/python" -m lkt.cli knowledge-status >/dev/null

printf 'Knowledge runtime ready: %s\n' "$VENV_DIR"
printf 'WordNet data ready: %s\n' "$WN_DATA_DIR"
