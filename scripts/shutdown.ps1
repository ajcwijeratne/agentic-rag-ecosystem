# =============================================================================
# AGENTIC RAG ECOSYSTEM — Graceful shutdown (Windows)
#   1. Stop Python services (.venv) via stop_all.ps1
#   2. Bring the Docker stack down (containers stop; named volumes are kept)
# Docker Desktop itself is left running.
# =============================================================================

$ProjectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $ProjectRoot

Write-Host "`n=== Stopping Python services ===" -ForegroundColor Cyan
& "$PSScriptRoot\stop_all.ps1"

Write-Host "`n=== Stopping Docker stack ===" -ForegroundColor Cyan
docker compose down
if ($LASTEXITCODE -ne 0) { docker-compose down }

Write-Host "`nEcosystem stopped. Data volumes (Qdrant, n8n, Ollama models) are preserved." -ForegroundColor Green
Start-Sleep -Seconds 3
