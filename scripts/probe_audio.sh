#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./scripts/probe_audio.sh
  ./scripts/probe_audio.sh --capture OUTPUT.wav [SECONDS]

Lists detected ALSA devices without changing configuration. Capture mode writes
one bounded mono 16 kHz WAV using the current default input.
EOF
}

capture_path=""
seconds=5
case "${1:-}" in
  "") ;;
  --capture)
    capture_path="${2:-}"
    seconds="${3:-5}"
    if [[ -z "$capture_path" || ! "$seconds" =~ ^[1-9][0-9]?$ ]]; then
      usage >&2
      exit 2
    fi
    ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

printf '%s\n' 'Kernel ALSA cards:'
if [[ -r /proc/asound/cards ]]; then
  sed 's/^/  /' /proc/asound/cards
else
  printf '%s\n' '  /proc/asound/cards is unavailable'
fi

printf '%s\n' 'Capture devices:'
if command -v arecord >/dev/null 2>&1; then
  arecord --list-devices || true
else
  printf '%s\n' '  arecord is unavailable; install the alsa-utils package'
  [[ -z "$capture_path" ]] || exit 3
fi

printf '%s\n' 'PipeWire sources:'
if command -v wpctl >/dev/null 2>&1; then
  wpctl status | sed -n '/Sources:/,/Streams:/p'
else
  printf '%s\n' '  wpctl is unavailable; ALSA detection above is still valid'
fi

if [[ -n "$capture_path" ]]; then
  mkdir -p "$(dirname "$capture_path")"
  printf 'Recording %s seconds to %s\n' "$seconds" "$capture_path"
  timeout "$((seconds + 3))" arecord \
    --file-type wav \
    --format S16_LE \
    --rate 16000 \
    --channels 1 \
    --duration "$seconds" \
    "$capture_path"
  test -s "$capture_path"
  if command -v file >/dev/null 2>&1; then
    file "$capture_path"
  else
    ls -lh "$capture_path"
  fi
fi

