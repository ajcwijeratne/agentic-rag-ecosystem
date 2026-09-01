# =============================================================================
# Download a VOSK speech-recognition model (Windows)
#   .\scripts\download_vosk_model.ps1
#   .\scripts\download_vosk_model.ps1 -ModelName vosk-model-en-us-0.22
#
# Model list: https://alphacephei.com/vosk/models
#   vosk-model-small-en-us-0.15   ~40 MB   fast, good enough for live captions
#   vosk-model-en-us-0.22         ~1.8 GB  accurate, needs ~4 GB RAM
# =============================================================================
param(
    [string]$ModelName = "vosk-model-small-en-us-0.15"
)

$ProjectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $ProjectRoot

if (Test-Path ".venv\Scripts\Activate.ps1") {
    . ".venv\Scripts\Activate.ps1"
}

Write-Host "[vosk] Downloading model: $ModelName" -ForegroundColor Cyan
python -m media.vosk_engine --download --model-name $ModelName

Write-Host ""
Write-Host "[vosk] Verifying..." -ForegroundColor Cyan
python -m media.vosk_engine --status
