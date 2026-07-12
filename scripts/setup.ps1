# =============================================================================
# Agentic RAG Ecosystem - Windows Setup Script
# Run once from the project root: .\scripts\setup.ps1
# Requires: Python 3.11+, Docker Desktop, Git
# =============================================================================

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path $PSScriptRoot -Parent

function Write-Step($msg) { Write-Host "[setup] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "[warn]  $msg" -ForegroundColor Yellow }

Set-Location $ProjectRoot

# ---------------------------------------------------------------------------
# 1. Python virtual environment
# ---------------------------------------------------------------------------
Write-Step "Creating Python virtual environment..."
python -m venv .venv
if (-not $?) { Write-Error "python -m venv failed. Is Python 3.11+ installed?" }

Write-Step "Activating virtual environment..."
& ".\.venv\Scripts\Activate.ps1"

Write-Step "Upgrading pip..."
python -m pip install --upgrade pip --quiet

Write-Step "Installing Python dependencies..."
pip install -r requirements.txt --quiet
Write-Step "Python dependencies installed."

# ---------------------------------------------------------------------------
# 2. Copy .env template
# ---------------------------------------------------------------------------
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Warn ".env created from template. Edit it with your API keys before starting."
} else {
    Write-Step ".env already exists - skipping."
}

# ---------------------------------------------------------------------------
# 3. Docker stack
# ---------------------------------------------------------------------------
$dockerAvailable = Get-Command docker -ErrorAction SilentlyContinue
if ($dockerAvailable) {
    Write-Step "Starting Docker stack (Qdrant, Ollama, n8n, SearXNG)..."
    docker compose up -d
    Write-Step "Docker stack started."

    Write-Step "Waiting for Ollama to become available..."
    $retries = 0
    while ($retries -lt 30) {
        try {
            $resp = Invoke-WebRequest "http://localhost:11434/api/tags" -UseBasicParsing -TimeoutSec 3 -ErrorAction SilentlyContinue
            if ($resp.StatusCode -eq 200) { break }
        } catch {}
        Start-Sleep 2
        $retries++
    }

    Write-Step "Pulling Ollama models (llama3, nomic-embed-text)..."
    docker exec ollama ollama pull llama3
    docker exec ollama ollama pull nomic-embed-text
    Write-Step "Ollama models ready."
} else {
    Write-Warn "Docker not found. Install Docker Desktop and re-run, or start services manually."
}

# ---------------------------------------------------------------------------
# 4. Create output directories
# ---------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path "transcripts" | Out-Null
New-Item -ItemType Directory -Force -Path "video_output" | Out-Null
New-Item -ItemType Directory -Force -Path "logs"         | Out-Null
New-Item -ItemType Directory -Force -Path "data"         | Out-Null

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Host ""
Write-Step "Setup complete! Next steps:"
Write-Host "  1. Edit .env - set API keys, OBSIDIAN_VAULT_PATH, WIJERCO_PATH"
Write-Host "  2. Run: .\.venv\Scripts\Activate.ps1"
Write-Host "  3. Run: .\scripts\start_all.ps1"
Write-Host "  4. Run: .\scripts\index_vault.ps1       (index Obsidian vault)"
Write-Host "  5. Run: .\scripts\index_wijerco.ps1     (index WijerCo knowledge base)"
Write-Host ""
Write-Host "  Services after start:"
Write-Host "    Orchestrator  -> http://localhost:8000/docs"
Write-Host "    n8n           -> http://localhost:5678"
Write-Host "    Qdrant        -> http://localhost:6333/dashboard"
Write-Host "    SearXNG       -> http://localhost:8080"
Write-Host "    Ollama        -> http://localhost:11434"
