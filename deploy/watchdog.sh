#!/usr/bin/env bash
# =============================================================================
# Watchdog. Run by rag-watchdog.timer every 5 minutes.
#
# Three checks:
#   1. Port liveness for every Python service; two consecutive failures
#      restart the owning systemd unit.
#   2. Daemon heartbeat freshness; stale beyond 10 minutes restarts rag-daemon.
#   3. Deep health on the orchestrator; a "down" dependency is logged and
#      notified (Docker services have their own restart policy).
#
# Failure counters live in /tmp/rag-watchdog so one blip never restarts
# anything; two in a row does.
# =============================================================================
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR=/tmp/rag-watchdog
mkdir -p "$STATE_DIR"

declare -A PORTS=(
  [rag-orchestrator]=8000
  [rag-local-agent]=8001
  [rag-search-agent]=8002
  [rag-cloud-agent]=8003
  [rag-notifier]=8004
  [rag-indexer]=8005
  [rag-retriever]=8006
)

log() { echo "$(date -Is) $*" >> "$REPO_DIR/logs/watchdog.log"; }

notify() {
  curl -s -m 10 -X POST http://localhost:8004/notify \
    -H 'Content-Type: application/json' \
    -d "{\"title\":\"Watchdog\",\"body\":\"$1\"}" >/dev/null 2>&1 || true
}

restart_unit() {
  local unit="$1" reason="$2"
  log "restarting $unit: $reason"
  sudo systemctl restart "$unit" 2>/dev/null || systemctl restart "$unit" 2>/dev/null
  notify "Restarted $unit ($reason)"
}

# --- 1. Port liveness, two strikes ------------------------------------------
for unit in "${!PORTS[@]}"; do
  port="${PORTS[$unit]}"
  # Only police units that are meant to be running.
  systemctl is-enabled --quiet "$unit.service" 2>/dev/null || continue
  counter="$STATE_DIR/$unit.fails"
  if curl -s -m 5 -o /dev/null "http://localhost:$port/health" \
     || curl -s -m 5 -o /dev/null "http://localhost:$port/"; then
    rm -f "$counter"
  else
    fails=$(( $(cat "$counter" 2>/dev/null || echo 0) + 1 ))
    echo "$fails" > "$counter"
    log "$unit port $port unresponsive (strike $fails)"
    if [ "$fails" -ge 2 ]; then
      restart_unit "$unit.service" "port $port down twice"
      rm -f "$counter"
    fi
  fi
done

# --- 2. Daemon heartbeat ------------------------------------------------------
HB="$REPO_DIR/logs/daemon_heartbeat"
if systemctl is-enabled --quiet rag-daemon.service 2>/dev/null; then
  if [ -f "$HB" ]; then
    age=$(( $(date +%s) - $(stat -c %Y "$HB") ))
    if [ "$age" -gt 600 ]; then
      restart_unit rag-daemon.service "heartbeat stale ${age}s"
    fi
  fi
fi

# --- 3. Deep health -----------------------------------------------------------
deep=$(curl -s -m 15 http://localhost:8000/health/deep 2>/dev/null || echo '')
if [ -n "$deep" ] && echo "$deep" | grep -q '"down"'; then
  counter="$STATE_DIR/deep.fails"
  fails=$(( $(cat "$counter" 2>/dev/null || echo 0) + 1 ))
  echo "$fails" > "$counter"
  log "deep health reports a down dependency (strike $fails)"
  if [ "$fails" -ge 2 ]; then
    notify "Deep health check reports a down dependency. Check /health/deep."
    rm -f "$counter"
  fi
else
  rm -f "$STATE_DIR/deep.fails"
fi

exit 0
