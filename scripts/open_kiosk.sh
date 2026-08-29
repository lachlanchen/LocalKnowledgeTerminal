#!/usr/bin/env bash
set -Eeuo pipefail

LKT_KIOSK_URL="${LKT_KIOSK_URL:-http://127.0.0.1:8090/?display}"
LKT_HEALTH_URL="${LKT_HEALTH_URL:-http://127.0.0.1:8090/api/health}"
LKT_RUNTIME_DIR="${LKT_RUNTIME_DIR:-${HOME}/LocalKnowledgeTerminal/runtime}"
PROFILE_DIR="${LKT_KIOSK_PROFILE:-${LKT_RUNTIME_DIR}/chromium-kiosk}"

if pgrep -f -- "--user-data-dir=${PROFILE_DIR}" >/dev/null 2>&1; then
  exit 0
fi

BROWSER=""
for candidate in chromium chromium-browser; do
  if command -v "$candidate" >/dev/null 2>&1; then
    BROWSER="$(command -v "$candidate")"
    break
  fi
done
[ -n "$BROWSER" ] || { echo "Chromium is required for the LKT kiosk" >&2; exit 1; }

for attempt in $(seq 1 120); do
  if curl --silent --fail "$LKT_HEALTH_URL" >/dev/null; then
    break
  fi
  if [ "$attempt" -eq 120 ]; then
    echo "LKT web service did not become ready" >&2
    exit 1
  fi
  sleep 1
done

mkdir -p "$PROFILE_DIR"
if [ -n "${WAYLAND_DISPLAY:-}" ]; then
  OZONE_PLATFORM="wayland"
else
  OZONE_PLATFORM="x11"
fi

exec "$BROWSER" \
  --ozone-platform="$OZONE_PLATFORM" \
  --kiosk \
  --no-first-run \
  --no-default-browser-check \
  --disable-session-crashed-bubble \
  --disable-translate \
  --noerrdialogs \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --user-data-dir="$PROFILE_DIR" \
  "$LKT_KIOSK_URL"
