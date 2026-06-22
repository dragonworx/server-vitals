#!/usr/bin/env bash
#
# Server Vitals uninstaller. Stops + disables the service and removes the
# installed files. Leaves the nginx snippet alone unless --purge-nginx is given
# (since it may still be `include`d by live server blocks).
#
# Usage:
#   sudo ./uninstall.sh [--purge-nginx]
#
set -euo pipefail

SERVICE_NAME="server-vitals"
BIN_DST="/usr/local/bin/server-vitals.py"
SVC_DST="/etc/systemd/system/${SERVICE_NAME}.service"
NGINX_SNIPPET_DST="/etc/nginx/snippets/server-vitals.conf"

PURGE_NGINX=0
[ "${1:-}" = "--purge-nginx" ] && PURGE_NGINX=1

log() { printf '\033[0;36m==>\033[0m %s\n' "$*"; }
ok()  { printf '\033[0;32m  ✓\033[0m %s\n' "$*"; }

# --- macOS / launchd uninstall -------------------------------------------------
# Mirror install-macos.sh: bootout + remove the plist and the binary, leaving the
# log in place. Pass --system to target a LaunchDaemon install (default: agent).
if [ "$(uname -s)" = "Darwin" ]; then
  LABEL="com.dragonworx.server-vitals"
  SYSTEM=0
  for a in "$@"; do [ "$a" = "--system" ] && SYSTEM=1; done
  if [ "$SYSTEM" -eq 1 ]; then
    PLIST="/Library/LaunchDaemons/${LABEL}.plist"; DOMAIN="system"; LC="sudo launchctl"
    LOG_FILE="/var/log/server-vitals.log"; PSUDO="sudo"
  else
    PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"; DOMAIN="gui/$(id -u)"; LC="launchctl"
    LOG_FILE="$HOME/Library/Logs/server-vitals.log"; PSUDO=""
  fi

  log "stopping + unloading $LABEL"
  $LC bootout "${DOMAIN}/${LABEL}" >/dev/null 2>&1 \
    || $LC bootout "$DOMAIN" "$PLIST" >/dev/null 2>&1 \
    || $LC unload -w "$PLIST" >/dev/null 2>&1 || true
  [ -f "$PLIST" ] && $PSUDO rm -f "$PLIST" && ok "removed $PLIST"

  # the binary may live in either dir install-macos.sh might have chosen
  for d in /usr/local/bin /opt/homebrew/bin; do
    if [ -f "$d/server-vitals.py" ]; then
      { [ -w "$d" ] && rm -f "$d/server-vitals.py"; } || sudo rm -f "$d/server-vitals.py"
      ok "removed $d/server-vitals.py"
    fi
  done

  [ -f "$LOG_FILE" ] && echo "    (left log in place: $LOG_FILE)"
  ok "uninstalled"
  exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
  exec sudo bash "$0" "$@"
fi

log "stopping + disabling $SERVICE_NAME"
systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true

rm -f "$SVC_DST" && ok "removed $SVC_DST"
rm -f "$BIN_DST" && ok "removed $BIN_DST"
systemctl daemon-reload

if [ "$PURGE_NGINX" -eq 1 ]; then
  rm -f "$NGINX_SNIPPET_DST" && ok "removed $NGINX_SNIPPET_DST"
  if command -v nginx >/dev/null 2>&1 && nginx -t >/dev/null 2>&1; then
    systemctl reload nginx && ok "nginx reloaded"
  fi
else
  echo "    (left nginx snippet in place; pass --purge-nginx to remove it)"
fi

ok "uninstalled"
