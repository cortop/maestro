#!/usr/bin/env bash
# Install (or remove) the maestro dispatcher as a launchd LaunchAgent.
# Normally invoked via `maestro fleet up`/`down` (maestro/fleet.py), which resolves this
# script from package data. Can also be run directly:
#   maestro/_assets/daemon/install.sh up     [--interval 300]
#   maestro/_assets/daemon/install.sh down
set -euo pipefail

ACTION="${1:-up}"
INTERVAL=300
[[ "${2:-}" == "--interval" ]] && INTERVAL="${3:-300}"

# Floor the cadence. A too-small interval is how the fleet burned 21,731 no-op
# reconciler sessions on 2026-07-19: it ran at ~10s instead of 300s, and nothing
# anywhere rejected the value. Keep this in sync with fleet.MIN_INTERVAL.
MIN_INTERVAL=60
if ! [[ "$INTERVAL" =~ ^[0-9]+$ ]] || (( INTERVAL < MIN_INTERVAL )); then
  echo "warning: interval '${INTERVAL}' below the ${MIN_INTERVAL}s floor — using ${MIN_INTERVAL}." >&2
  INTERVAL=$MIN_INTERVAL
fi

LEGACY_LABEL="com.maestro.dispatcher"
DEFAULT_HOME="$HOME/.maestro"
MAESTRO_HOME_RAW="${MAESTRO_HOME:-}"
MAESTRO_HOME="${MAESTRO_HOME:-$DEFAULT_HOME}"

# The label is derived per-home so a second MAESTRO_HOME's `up` doesn't clobber
# the first home's plist/launchd job. Only maestro/fleet.py computes the slug
# (fleet.label) -- this script never duplicates that hash, it just consumes
# MAESTRO_LABEL. The default home keeps the bare legacy label for backward
# compatibility with existing installs.
if [[ -n "${MAESTRO_LABEL:-}" ]]; then
  LABEL="$MAESTRO_LABEL"
elif [[ -z "$MAESTRO_HOME_RAW" || "$MAESTRO_HOME" == "$DEFAULT_HOME" ]]; then
  LABEL="$LEGACY_LABEL"
else
  echo "error: MAESTRO_HOME is set to a non-default home ($MAESTRO_HOME) but no MAESTRO_LABEL was given." >&2
  echo "Run 'maestro fleet up' instead -- it derives the per-home label automatically." >&2
  exit 1
fi

PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
HERE="$(cd "$(dirname "$0")" && pwd)"
MAESTRO_BIN="$(command -v maestro || true)"

if [[ "$ACTION" == "down" ]]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "maestro dispatcher unloaded."
  exit 0
fi

if [[ -z "$MAESTRO_BIN" ]]; then
  echo "error: 'maestro' not on PATH. Install it first: pip install -e .  (or pipx install)." >&2
  exit 1
fi

# launchd runs with a minimal PATH that omits ~/.local/bin, so the dispatcher
# can't find `maestro`/`claude` at spawn time (FileNotFoundError -> exit 1).
# Pin the dirs where they actually live, then the usual system locations.
CLAUDE_BIN="$(command -v claude || true)"
if [[ -z "$CLAUDE_BIN" ]]; then
  echo "warning: 'claude' not on PATH — the dispatcher will fail to spawn reconcilers." >&2
  CLAUDE_BIN="$HOME/.local/bin/claude"
fi
LAUNCHD_PATH="$(dirname "$MAESTRO_BIN"):$(dirname "$CLAUDE_BIN"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

mkdir -p "$HOME/Library/LaunchAgents" "$MAESTRO_HOME/agent-logs"
sed -e "s#@MAESTRO_BIN@#${MAESTRO_BIN}#g" \
    -e "s#@MAESTRO_HOME@#${MAESTRO_HOME}#g" \
    -e "s#@PATH@#${LAUNCHD_PATH}#g" \
    -e "s#@LABEL@#${LABEL}#g" \
    -e "s#<integer>300</integer>#<integer>${INTERVAL}</integer>#g" \
    "$HERE/com.maestro.dispatcher.plist.template" > "$PLIST"

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "maestro dispatcher loaded (every ${INTERVAL}s). Logs: $MAESTRO_HOME/agent-logs/"
echo "Verify: maestro doctor"
