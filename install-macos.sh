#!/usr/bin/env bash
#
# Server Vitals installer — macOS / launchd.
#
# Installs server-vitals.py as a launchd-managed background service that binds
# 127.0.0.1:9999 (nothing is reachable off your Mac) and respawns if it dies:
#   - copies server-vitals.py -> /usr/local/bin/ (mode 0755; sudo only if needed)
#   - writes a launchd plist (label com.dragonworx.server-vitals) and loads it
#   - polls /health and reports whether the service actually came up
#
# By default it installs a per-user LaunchAgent (~/Library/LaunchAgents) that
# runs as you and needs no sudo. Pass --system for a system-wide LaunchDaemon
# (/Library/LaunchDaemons) that runs as root at boot (sudo). The agent is the
# right default — the endpoint is local-only, so root/boot buys nothing.
#
# This script is normally reached via ./install.sh, which dispatches here on
# macOS. Run it directly if you like:
#   ./install-macos.sh            # LaunchAgent;  or: make install
#   ./install-macos.sh --system   # LaunchDaemon; or: make install ARGS=--system
#
# Options:
#   --system    install a system-wide LaunchDaemon (root, /Library/LaunchDaemons)
#   --no-start  install files but don't load/start the service
#   -h, --help  show this help
#
set -euo pipefail

LABEL="com.dragonworx.server-vitals"
HEALTH_URL="http://127.0.0.1:9999/health"
PYTHON="/usr/bin/python3"

SYSTEM=0
DO_START=1

log()  { printf '\033[0;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[0;32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m  ! \033[0m%s\n' "$*" >&2; }
die()  { printf '\033[0;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

[ "$(uname -s)" = "Darwin" ] || die "install-macos.sh targets macOS; on Linux use ./install.sh"

# --- parse args ----------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --system)   SYSTEM=1 ;;
    --no-start) DO_START=0 ;;
    -h|--help)  usage ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

command -v "$PYTHON" >/dev/null 2>&1 || PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] && [ -x "$PYTHON" ] || die "python3 not found on PATH"

# --- pick the binary dir: default /usr/local/bin; on Apple Silicon fall back to
#     /opt/homebrew/bin only when it's on PATH and /usr/local/bin is absent. -----
BIN_DIR="/usr/local/bin"
if [ ! -d "$BIN_DIR" ]; then
  case ":$PATH:" in *:/opt/homebrew/bin:*) BIN_DIR="/opt/homebrew/bin" ;; esac
fi
BIN_DST="$BIN_DIR/server-vitals.py"

# --- domain / plist / log paths per install kind -------------------------------
if [ "$SYSTEM" -eq 1 ]; then
  PLIST="/Library/LaunchDaemons/${LABEL}.plist"
  LOG_FILE="/var/log/server-vitals.log"
  DOMAIN="system"
  LC="sudo launchctl"
  KIND="LaunchDaemon"
else
  PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
  LOG_FILE="$HOME/Library/Logs/server-vitals.log"
  DOMAIN="gui/$(id -u)"
  LC="launchctl"
  KIND="LaunchAgent"
fi
SVC_TARGET="${DOMAIN}/${LABEL}"

# --- locate source -------------------------------------------------------------
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo /nonexistent)"
[ -f "$SRC_DIR/server-vitals.py" ] || die "can't find server-vitals.py next to the installer"

# --- install the binary (sudo only when the dir isn't writable) ----------------
SUDO_BIN=""
if [ -d "$BIN_DIR" ]; then
  [ -w "$BIN_DIR" ] || SUDO_BIN="sudo"
else
  parent="$(dirname "$BIN_DIR")"
  { [ -d "$parent" ] && [ -w "$parent" ]; } || SUDO_BIN="sudo"
fi
log "installing binary -> $BIN_DST"
[ -n "$SUDO_BIN" ] && log "  $BIN_DIR not writable — using sudo"
$SUDO_BIN mkdir -p "$BIN_DIR"
$SUDO_BIN install -m 0755 "$SRC_DIR/server-vitals.py" "$BIN_DST"
ok "binary installed"

# --- write + validate the plist ------------------------------------------------
PLIST_SUDO=""
[ "$SYSTEM" -eq 1 ] && PLIST_SUDO="sudo"
log "writing plist -> $PLIST"
tmp_plist="$(mktemp -t server-vitals-plist)"
cat > "$tmp_plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>             <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${BIN_DST}</string>
    </array>
    <key>RunAtLoad</key>         <true/>
    <key>KeepAlive</key>         <true/>
    <key>WorkingDirectory</key>  <string>/tmp</string>
    <key>StandardOutPath</key>   <string>${LOG_FILE}</string>
    <key>StandardErrorPath</key> <string>${LOG_FILE}</string>
</dict>
</plist>
EOF

plutil -lint "$tmp_plist" >/dev/null || { rm -f "$tmp_plist"; die "generated plist failed plutil -lint"; }
$PLIST_SUDO mkdir -p "$(dirname "$PLIST")"
$PLIST_SUDO install -m 0644 "$tmp_plist" "$PLIST"
rm -f "$tmp_plist"
# launchd refuses to write the log if the dir is missing (mainly the --system case)
$PLIST_SUDO mkdir -p "$(dirname "$LOG_FILE")"
ok "plist installed"

# --- load (idempotent: tear down any previous instance first) ------------------
if [ "$DO_START" -eq 1 ]; then
  log "loading $KIND -> $SVC_TARGET"
  # bootout the old instance so re-installs don't trip "service already loaded"
  $LC bootout "$SVC_TARGET" >/dev/null 2>&1 \
    || $LC bootout "$DOMAIN" "$PLIST" >/dev/null 2>&1 \
    || $LC unload -w "$PLIST" >/dev/null 2>&1 || true
  # modern API first, fall back to legacy load on older macOS
  if ! $LC bootstrap "$DOMAIN" "$PLIST" >/dev/null 2>&1; then
    $LC load -w "$PLIST" || die "launchctl failed to load $PLIST"
  fi
  ok "service loaded"

  # poll /health for up to ~5s
  up=0
  for _ in 1 2 3 4 5; do
    if curl -fsS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1; then up=1; break; fi
    sleep 1
  done
  if [ "$up" -eq 1 ]; then
    ok "health endpoint responding at $HEALTH_URL"
  else
    warn "service loaded but $HEALTH_URL did not respond within 5s"
    if [ -f "$LOG_FILE" ]; then
      warn "last lines of $LOG_FILE:"
      tail -n 20 "$LOG_FILE" >&2 || true
    fi
    die "service failed to come up"
  fi
else
  warn "skipped load (--no-start); load later with: $LC bootstrap $DOMAIN $PLIST"
fi

echo
ok "done. endpoints (on 127.0.0.1:9999): /health  /stats"
echo "    stats UI: http://127.0.0.1:9999/stats"
echo "    logs:     tail -f $LOG_FILE   (or: make logs)"
echo "    manage:   make start | stop | restart | status | logs"
