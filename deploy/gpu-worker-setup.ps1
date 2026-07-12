# =============================================================================
# GPU worker setup for the Windows PC. Run in PowerShell from the repo root:
#
#   powershell -ExecutionPolicy Bypass -File deploy\gpu-worker-setup.ps1
#
# Installs the two free clone engines and their worker servers:
#   * F5-TTS voice clone      -> gpu_workers\voice_worker.py   (port 8020)
#   * SadTalker avatar        -> gpu_workers\avatar_worker.py  (port 7861)
#
# Requirements: NVIDIA GPU with current drivers, Python 3.10+, git.
# Roughly 15 GB disk for models. No accounts, no API keys, no charges.
# =============================================================================
$ErrorActionPreference = "Stop"
$RepoDir = Split-Path $PSScriptRoot -Parent
Set-Location $RepoDir

Write-Host "[0/4] GPU check" -ForegroundColor Cyan
try {
    $gpu = nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
    Write-Host "  $gpu"
    $vramMB = [int]((nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits) | Select-Object -First 1)
    if ($vramMB -lt 6000) {
        Write-Host "  Under 6GB VRAM: voice clone will work; SadTalker may need --still mode." -ForegroundColor Yellow
    }
} catch {
    Write-Host "  nvidia-smi not found. Install NVIDIA drivers first." -ForegroundColor Red
    exit 1
}

Write-Host "[1/4] Worker venv" -ForegroundColor Cyan
$WorkerVenv = Join-Path $RepoDir "gpu_workers\.venv"
if (-not (Test-Path "$WorkerVenv\Scripts\python.exe")) {
    python -m venv $WorkerVenv
}
$Py = "$WorkerVenv\Scripts\python.exe"
& $Py -m pip install --quiet --upgrade pip
& $Py -m pip install --quiet fastapi uvicorn httpx

Write-Host "[2/4] F5-TTS (voice clone, MIT licence)" -ForegroundColor Cyan
# Torch with CUDA first so f5-tts does not pull the CPU build.
& $Py -m pip install --quiet torch torchaudio --index-url https://download.pytorch.org/whl/cu124
& $Py -m pip install --quiet f5-tts
Write-Host "  Model weights download automatically on first render (~1.4 GB)."

Write-Host "[3/4] SadTalker (avatar)" -ForegroundColor Cyan
$SadTalker = Join-Path $env:USERPROFILE "SadTalker"
if (-not (Test-Path $SadTalker)) {
    git clone https://github.com/OpenTalker/SadTalker $SadTalker
}
& $Py -m pip install --quiet -r "$SadTalker\requirements.txt"
$Checkpoints = Join-Path $SadTalker "checkpoints"
if (-not (Test-Path $Checkpoints)) {
    Write-Host "  Downloading SadTalker checkpoints (~4 GB)..."
    New-Item -ItemType Directory -Force -Path $Checkpoints | Out-Null
    $base = "https://github.com/OpenTalker/SadTalker/releases/download/v0.0.2-rc"
    foreach ($f in @("mapping_00109-model.pth.tar", "mapping_00229-model.pth.tar", "SadTalker_V0.0.2_256.safetensors", "SadTalker_V0.0.2_512.safetensors")) {
        Invoke-WebRequest -Uri "$base/$f" -OutFile (Join-Path $Checkpoints $f)
    }
}

Write-Host "[4/4] Start commands" -ForegroundColor Cyan
Write-Host @"

Set these in a .env or as user environment variables on this PC:
  VOICE_REF_AUDIO=C:\path\to\your-voice-reference.wav   (5-15s clean clip of you)
  VOICE_REF_TEXT=<exact transcript of that clip>
  AVATAR_PORTRAIT=C:\path\to\your-portrait.jpg
  SADTALKER_DIR=$SadTalker

Start the workers (two terminals, or register with NSSM as services):
  $Py -m gpu_workers.voice_worker
  $Py -m gpu_workers.avatar_worker

Then on the machine running the orchestrator, set:
  F5_TTS_URL=http://<this-pc-tailscale-ip>:8020
  MEDIA_TOOL_SADTALKER_ENDPOINT=http://<this-pc-tailscale-ip>:7861
  MEDIA_TOOL_DEFAULT_VOICE=f5-tts
  MEDIA_TOOL_DEFAULT_AVATAR=sadtalker

Verify from the orchestrator machine:
  python -m scripts.verify_media_providers
"@
