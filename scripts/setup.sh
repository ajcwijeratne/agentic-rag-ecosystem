#!/usr/bin/env bash
# =============================================================================
# Agentic RAG Ecosystem — Bootstrap Script
# Run once after cloning: bash scripts/setup.sh
# =============================================================================

set -euo pipefail

GREEN="\033[0;32m"
YELLOW="\033[1;33m"
NC="\033[0m"

log()  { echo -e "${GREEN}[setup]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC}  $*"; }

# ---------------------------------------------------------------------------
# 1. Python virtual environment
# ---------------------------------------------------------------------------
log "Creating Python virtual environment..."
python3 -m venv .venv
source .venv/bin/activate

log "Upgrading pip..."
pip install --upgrade pip --quiet

log "Installing Python dependencies..."
pip install -r requirements.txt --quiet
log "Python dependencies installed."

# ---------------------------------------------------------------------------
# 2. Copy .env template
# ---------------------------------------------------------------------------
if [ ! -f .env ]; then
  cp .env.example .env
  warn ".env created from template. Edit it before starting services."
else
  log ".env already exists — skipping."
fi

# ---------------------------------------------------------------------------
# 3. Docker stack
# ---------------------------------------------------------------------------
if command -v docker &>/dev/null && command -v docker-compose &>/dev/null; then
  log "Starting Docker stack (Qdrant, Ollama, n8n, SearXNG)..."
  docker-compose up -d
  log "Docker stack started."

  # Wait for Ollama to be ready, then pull models
  log "Waiting for Ollama to become available..."
  for i in {1..30}; do
    if curl -sf http://localhost:11434/api/tags &>/dev/null; then
      break
    fi
    sleep 2
  done

  log "Pulling Ollama models (llama3, nomic-embed-text)..."
  docker exec ollama ollama pull llama3       || warn "Could not pull llama3"
  docker exec ollama ollama pull nomic-embed-text || warn "Could not pull nomic-embed-text"
  log "Ollama models ready."
else
  warn "Docker or docker-compose not found. Start services manually."
fi

# ---------------------------------------------------------------------------
# 4. Create output directories
# ---------------------------------------------------------------------------
mkdir -p transcripts video_output

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
log "Setup complete! Next steps:"
echo "  1. Edit .env with your API keys and paths"
echo "  2. source .venv/bin/activate"
echo "  3. bash scripts/start_all.sh"
echo ""
echo "  Services:"
echo "    Orchestrator  → http://localhost:8000"
echo "    n8n           → http://localhost:5678"
echo "    Qdrant        → http://localhost:6333/dashboard"
echo "    SearXNG       → http://localhost:8080"
echo "    Ollama        → http://localhost:11434"
