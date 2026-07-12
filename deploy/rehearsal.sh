#!/usr/bin/env bash
# =============================================================================
# Weekly operational rehearsal. Run by rag-rehearsal.timer every Monday 05:30,
# or by hand: bash deploy/rehearsal.sh
#
# Follows the flow in docs/hardening-operational-rehearsal.md:
#   migrate -> backup -> restore (dry run) -> release snapshot ->
#   rollback (dry run) -> monitoring -> rehearsal verdict
#
# The result lands in logs/rehearsal.log and a summary is sent through the
# notifier, so the Monday brief carries a pass or a needs_attention you did
# not have to run yourself. Exits non-zero when attention is needed.
# =============================================================================
set -uo pipefail

BASE="${ORCHESTRATOR_URL:-http://localhost:8000}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$REPO_DIR/logs/rehearsal.log"
mkdir -p "$REPO_DIR/logs"

HDR=(-H "Content-Type: application/json")
if [ -n "${ADMIN_API_KEY:-}" ]; then HDR+=(-H "X-API-Key: $ADMIN_API_KEY");
elif [ -n "${API_KEY:-}" ]; then HDR+=(-H "X-API-Key: $API_KEY"); fi

log() { echo "$(date -Is) $*" | tee -a "$LOG"; }

step() {
  local name="$1" method="$2" path="$3" body="${4:-}"
  local out
  if [ -n "$body" ]; then
    out=$(curl -s -m 120 -X "$method" "${HDR[@]}" -d "$body" "$BASE$path" 2>&1)
  else
    out=$(curl -s -m 120 -X "$method" "${HDR[@]}" "$BASE$path" 2>&1)
  fi
  log "[$name] ${out:0:400}"
  echo "$out"
}

notify() {
  curl -s -m 10 -X POST http://localhost:8004/notify \
    -H 'Content-Type: application/json' \
    -d "{\"title\":\"Weekly rehearsal\",\"body\":\"$1\"}" >/dev/null 2>&1 || true
}

log "=== rehearsal start ==="

step "migrate"  POST /ops/migrate
step "backup"   POST /ops/backup
step "restore-dry"  POST /ops/restore '{"dry_run": true}'
step "snapshot" POST /ops/releases/snapshot
step "rollback-dry" POST /ops/releases/rollback '{"dry_run": true}'
step "monitoring" GET /ops/monitoring

verdict=$(step "rehearsal" GET /ops/rehearsal)

if echo "$verdict" | grep -q 'needs_attention'; then
  log "=== rehearsal verdict: NEEDS ATTENTION ==="
  notify "needs_attention. Check /ops/rehearsal and logs/rehearsal.log."
  exit 1
fi

log "=== rehearsal verdict: pass ==="
notify "Pass. Migrate, backup, restore dry run, snapshot, rollback dry run all clean."
exit 0
