#!/usr/bin/env bash
# =============================================================================
# Start all Python services in the background.
# Run from the project root after activating .venv.
# =============================================================================

set -euo pipefail

source .venv/bin/activate 2>/dev/null || true
set -a; source .env 2>/dev/null || true; set +a

LOG_DIR="./logs"
mkdir -p "$LOG_DIR"

start_service() {
  local name="$1"
  local cmd="$2"
  local log_file="$LOG_DIR/${name}.log"
  echo "[start] $name → $log_file"
  nohup bash -c "$cmd" > "$log_file" 2>&1 &
  echo $! > "$LOG_DIR/${name}.pid"
}

# Core orchestrator
start_service "orchestrator" "python -m orchestrator.main"

# FastMCP sub-agents
start_service "local_data_agent" "python -m agents.local_data_agent"
start_service "search_agent"     "python -m agents.search_agent"
start_service "cloud_agent"      "python -m agents.cloud_agent"

# RAG services
start_service "indexer"   "python -m rag.indexer --serve"
start_service "retriever" "python -m rag.retriever"

# Notifier
start_service "notifier" "python -m notifications.notifier --serve"

# Media (start on demand — uncomment to autostart)
# start_service "whisper"  "python -m media.whisper_pipeline --serve"
# start_service "video"    "python -m media.video_pipeline --serve"

echo ""
echo "All services started. Logs in $LOG_DIR/"
echo "  Orchestrator API → http://localhost:8000/docs"
