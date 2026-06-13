#!/usr/bin/env bash
# Install (or remove) the maestro dispatcher as a launchd LaunchAgent.
# Usage:
#   daemon/install.sh up     [--interval 300]
#   daemon/install.sh down
set -euo pipefail

ACTION="${1:-up}"
INTERVAL=300
[[ "${2:-}" == "--interval" ]] && INTERVAL="${3:-300}"

LABEL="com.maestro.dispatcher"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
HERE="$(cd "$(dirname "$0")" && pwd)"
MAESTRO_HOME="${MAESTRO_HOME:-$HOME/.maestro}"
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
    -e "s#<integer>300</integer>#<integer>${INTERVAL}</integer>#g" \
    "$HERE/com.maestro.dispatcher.plist.template" > "$PLIST"

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "maestro dispatcher loaded (every ${INTERVAL}s). Logs: $MAESTRO_HOME/agent-logs/"
echo "Verify: maestro doctor"
