#!/usr/bin/env bash
#
# vitals installer.
#
# Installs the vitals server health endpoint as a systemd service:
#   - copies vitals.py                   -> /usr/local/bin/
#   - installs vitals.service            -> /etc/systemd/system/
#   - (optional) installs the nginx snippet for reverse-proxying the endpoints
#   - reloads systemd, enables + starts the service, and verifies it responds
#
# Usage (from a checkout):
#   sudo ./install.sh [options]
#
# Usage (piped from the internet — set the raw base URL of the repo):
#   curl -fsSL <raw>/install.sh | VITALS_RAW_BASE=<raw> sudo -E bash
#
# Options:
#   --with-nginx     also install the nginx snippet and reload nginx
#   --no-start       install files but don't enable/start the service
#   --user USER      run the service as USER (default: www-data)
#   -h, --help       show this help
#
set -euo pipefail

SERVICE_NAME="vitals"
BIN_DST="/usr/local/bin/vitals.py"
SVC_DST="/etc/systemd/system/${SERVICE_NAME}.service"
NGINX_SNIPPET_DST="/etc/nginx/snippets/vitals.conf"
HEALTH_URL="http://127.0.0.1:9999/health"

RUN_USER="www-data"
WITH_NGINX=0
DO_START=1

log()  { printf '\033[0;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[0;32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m  ! \033[0m%s\n' "$*" >&2; }
die()  { printf '\033[0;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

# --- parse args ----------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --with-nginx) WITH_NGINX=1 ;;
    --no-start)   DO_START=0 ;;
    --user)       RUN_USER="${2:?--user needs a value}"; shift ;;
    -h|--help)    usage ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

# --- need root -----------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
  log "installer needs root — re-running under sudo"
  exec sudo -E bash "$0" \
    $([ "$WITH_NGINX" -eq 1 ] && echo --with-nginx) \
    $([ "$DO_START" -eq 0 ] && echo --no-start) \
    --user "$RUN_USER"
fi

command -v python3 >/dev/null 2>&1 || die "python3 not found on PATH"
command -v systemctl >/dev/null 2>&1 || die "systemctl not found (this installer targets systemd)"

# --- locate sources (checkout dir, or fetch when piped) ------------------------
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo /nonexistent)"
TMP_SRC=""
cleanup() { [ -n "$TMP_SRC" ] && rm -rf "$TMP_SRC"; return 0; }
trap cleanup EXIT

if [ ! -f "$SRC_DIR/vitals.py" ]; then
  [ -n "${VITALS_RAW_BASE:-}" ] || \
    die "can't find vitals.py next to the installer; set VITALS_RAW_BASE to fetch it"
  log "fetching sources from $VITALS_RAW_BASE"
  TMP_SRC="$(mktemp -d)"
  for f in vitals.py vitals.service nginx/vitals.conf; do
    mkdir -p "$TMP_SRC/$(dirname "$f")"
    curl -fsSL "${VITALS_RAW_BASE%/}/$f" -o "$TMP_SRC/$f" || die "failed to fetch $f"
  done
  SRC_DIR="$TMP_SRC"
fi

# --- install binary ------------------------------------------------------------
log "installing binary -> $BIN_DST"
install -m 0755 "$SRC_DIR/vitals.py" "$BIN_DST"
ok "binary installed"

# --- install systemd unit (with the requested run user) ------------------------
log "installing service -> $SVC_DST (user: $RUN_USER)"
tmp_unit="$(mktemp)"
sed -e "s/^User=.*/User=${RUN_USER}/" \
    -e "s/^Group=.*/Group=${RUN_USER}/" \
    "$SRC_DIR/vitals.service" > "$tmp_unit"
install -m 0644 "$tmp_unit" "$SVC_DST"
rm -f "$tmp_unit"
systemctl daemon-reload
ok "service installed"

# --- optional nginx snippet ----------------------------------------------------
if [ "$WITH_NGINX" -eq 1 ]; then
  log "installing nginx snippet -> $NGINX_SNIPPET_DST"
  install -D -m 0644 "$SRC_DIR/nginx/vitals.conf" "$NGINX_SNIPPET_DST"
  if command -v nginx >/dev/null 2>&1 && nginx -t >/dev/null 2>&1; then
    systemctl reload nginx && ok "nginx reloaded"
  else
    warn "nginx config test failed or nginx absent — snippet copied but not reloaded"
  fi
  warn "add this to the server blocks that should expose the endpoints:"
  warn "    include snippets/vitals.conf;"
fi

# --- enable + start ------------------------------------------------------------
if [ "$DO_START" -eq 1 ]; then
  log "enabling + starting $SERVICE_NAME"
  systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
  systemctl restart "$SERVICE_NAME"
  sleep 1
  if systemctl is-active --quiet "$SERVICE_NAME"; then
    ok "service is active"
  else
    die "service failed to start — see: journalctl -u $SERVICE_NAME -n 50"
  fi
  if curl -fsS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1; then
    ok "health endpoint responding at $HEALTH_URL"
  else
    warn "service is up but $HEALTH_URL did not respond yet"
  fi
else
  warn "skipped start (--no-start); run: sudo systemctl enable --now $SERVICE_NAME"
fi

echo
ok "done. endpoints (on 127.0.0.1:9999): /health  /code-server  /stats"
echo "    logs:    journalctl -u $SERVICE_NAME -f"
echo "    stats UI: http://127.0.0.1:9999/stats"
