#!/usr/bin/env bash
#
# vitals uninstaller. Stops + disables the service and removes the
# installed files. Leaves the nginx snippet alone unless --purge-nginx is given
# (since it may still be `include`d by live server blocks).
#
# Usage:
#   sudo ./uninstall.sh [--purge-nginx]
#
set -euo pipefail

SERVICE_NAME="vitals"
BIN_DST="/usr/local/bin/vitals.py"
SVC_DST="/etc/systemd/system/${SERVICE_NAME}.service"
NGINX_SNIPPET_DST="/etc/nginx/snippets/vitals.conf"

PURGE_NGINX=0
[ "${1:-}" = "--purge-nginx" ] && PURGE_NGINX=1

log() { printf '\033[0;36m==>\033[0m %s\n' "$*"; }
ok()  { printf '\033[0;32m  ✓\033[0m %s\n' "$*"; }

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
