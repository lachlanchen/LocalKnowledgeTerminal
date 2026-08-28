#!/usr/bin/env bash
set -Eeuo pipefail

BASE_URL="${LKT_URL:-http://127.0.0.1:8090}"
curl --fail --silent --show-error "${BASE_URL}/api/health"
printf '\n'
curl --fail --silent --show-error \
  -H 'Content-Type: application/json' \
  --data '{"query":"abacus","mode":"word"}' \
  "${BASE_URL}/api/cards"
printf '\n'
