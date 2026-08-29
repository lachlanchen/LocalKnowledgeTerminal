#!/usr/bin/env bash
set -Eeuo pipefail

[ "${EUID:-$(id -u)}" -eq 0 ] || {
  echo "Run with sudo" >&2
  exit 1
}

LKT_USER="${LKT_USER:-lachlan}"
SOURCE_DIR="${LKT_SOURCE:-/home/${LKT_USER}/LocalKnowledgeTerminal/source}"
ACTION="${1:---start}"

case "$ACTION" in
  --start|--restart) ;;
  *) echo "Usage: sudo $0 [--start|--restart]" >&2; exit 2 ;;
esac

LKT_USER_HOME="$(getent passwd "$LKT_USER" | cut -d: -f6)"
[ -n "$LKT_USER_HOME" ] || { echo "Unknown LKT user: $LKT_USER" >&2; exit 1; }

for path in \
  systemd/lkt-llm.service \
  systemd/lkt-web.service \
  systemd/lkt-worker.service \
  scripts/open_kiosk.sh \
  desktop/lkt-kiosk.desktop; do
  [ -f "$SOURCE_DIR/$path" ] || { echo "Missing $SOURCE_DIR/$path" >&2; exit 1; }
done

bash -n "$SOURCE_DIR/scripts/open_kiosk.sh"
if command -v desktop-file-validate >/dev/null 2>&1; then
  desktop-file-validate "$SOURCE_DIR/desktop/lkt-kiosk.desktop"
fi

install -o root -g root -m 0644 "$SOURCE_DIR/systemd/lkt-llm.service" /etc/systemd/system/
install -o root -g root -m 0644 "$SOURCE_DIR/systemd/lkt-web.service" /etc/systemd/system/
install -o root -g root -m 0644 "$SOURCE_DIR/systemd/lkt-worker.service" /etc/systemd/system/
install -o root -g root -m 0755 "$SOURCE_DIR/scripts/open_kiosk.sh" /usr/local/bin/lkt-open-kiosk
install -d -o "$LKT_USER" -g "$LKT_USER" -m 0755 "$LKT_USER_HOME/.config/autostart"
install -o "$LKT_USER" -g "$LKT_USER" -m 0644 \
  "$SOURCE_DIR/desktop/lkt-kiosk.desktop" \
  "$LKT_USER_HOME/.config/autostart/lkt-kiosk.desktop"

systemctl daemon-reload
systemctl enable lkt-llm.service lkt-web.service lkt-worker.service >/dev/null
if systemctl list-unit-files lightdm.service --no-legend 2>/dev/null | grep -q '^lightdm.service'; then
  systemctl enable lightdm.service >/dev/null
fi

if [ "$ACTION" = "--restart" ]; then
  systemctl stop lkt-worker.service lkt-web.service
  systemctl restart lkt-llm.service
else
  systemctl start lkt-llm.service
fi

for attempt in $(seq 1 180); do
  if curl --silent --fail http://127.0.0.1:8081/health >/dev/null; then
    break
  fi
  if [ "$attempt" -eq 180 ]; then
    echo "Local model service did not become ready" >&2
    journalctl -u lkt-llm.service -n 80 --no-pager >&2
    exit 1
  fi
  sleep 2
done

if [ "$ACTION" = "--restart" ]; then
  systemctl restart lkt-web.service
else
  systemctl start lkt-web.service
fi

for attempt in $(seq 1 60); do
  health="$(curl --silent --fail http://127.0.0.1:8090/api/health || true)"
  if printf '%s' "$health" | python3 -c \
    'import json,sys; raise SystemExit(json.load(sys.stdin).get("status") != "ready")' \
    2>/dev/null; then
    break
  fi
  if [ "$attempt" -eq 60 ]; then
    echo "LKT web service did not report ready" >&2
    journalctl -u lkt-web.service -n 80 --no-pager >&2
    exit 1
  fi
  sleep 2
done

if [ "$ACTION" = "--restart" ]; then
  systemctl restart lkt-worker.service
else
  systemctl start lkt-worker.service
fi

for unit in lkt-llm.service lkt-web.service lkt-worker.service; do
  systemctl is-enabled --quiet "$unit"
  systemctl is-active --quiet "$unit"
done

printf 'LKT services enabled and active; desktop autostart installed for %s.\n' "$LKT_USER"
printf '%s\n' "$health"
