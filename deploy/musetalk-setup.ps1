# =============================================================================
# MuseTalk avatar setup for the Windows GPU PC. Run in PowerShell from the repo
# root ON THE MACHINE WITH THE NVIDIA GPU (not the mini PC):
#
#   powershell -ExecutionPolicy Bypass -File deploy\musetalk-setup.ps1
#
# Installs the MuseTalk lip-sync engine behind gpu_workers\musetalk_worker.py
# (port 7862 by default, so it runs alongside SadTalker on 7861). MuseTalk
# lip-syncs a reference video of you to narration -- higher quality than the
# SadTalker single-photo animation.
#
# Requirements: NVIDIA GPU with current drivers, Python 3.10+, git, ffmpeg.
# Roughly 10 GB disk for models. Free, local, no API keys.
# =============================================================================
$ErrorActionPreference = "Stop"
$RepoDir = Split-Path $PSScriptRoot -Parent
Set-Location $RepoDir

Write-Host "[0/4] GPU check" -ForegroundColor Cyan
try {
    $gpu = nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
    Write-Host "  $gpu"
} catch {
    Write-Host "  nvidia-smi not found. MuseTalk needs an NVIDIA GPU. Run this on the GPU PC." -ForegroundColor Red
    exit 1
}

Write-Host "[1/4] Reuse the GPU worker venv (fastapi/uvicorn/httpx)" -ForegroundColor Cyan
$WorkerVenv = Join-Path $RepoDir "gpu_workers\.venv"
if (-not (Test-Path "$WorkerVenv\Scripts\python.exe")) { python -m venv $WorkerVenv }
$Py = "$WorkerVenv\Scripts\python.exe"
& $Py -m pip install --quiet --upgrade pip
& $Py -m pip install --quiet fastapi uvicorn httpx

Write-Host "[2/4] Clone MuseTalk + install requirements" -ForegroundColor Cyan
$MuseTalk = Join-Path $env:USERPROFILE "MuseTalk"
if (-not (Test-Path $MuseTalk)) {
    git clone https://github.com/TMElyralab/MuseTalk $MuseTalk
}
# CUDA torch first so requirements don't pull the CPU build.
& $Py -m pip install --quiet torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
& $Py -m pip install --quiet -r "$MuseTalk\requirements.txt"
# MuseTalk also needs mmlab packages and ffmpeg on PATH; see its README if the
# next step reports a missing mmcv/mmpose/mmdet.

Write-Host "[3/4] Download MuseTalk model weights (~10 GB)" -ForegroundColor Cyan
$weights = Join-Path $MuseTalk "models"
if ((Test-Path $weights) -and (Get-ChildItem $weights -Recurse -File | Measure-Object).Count -gt 3) {
    Write-Host "  models/ already populated, skipping."
} else {
    $sh = Join-Path $MuseTalk "download_weights.sh"
    $bat = Join-Path $MuseTalk "download_weights.bat"
    if (Test-Path $bat) {
        Push-Location $MuseTalk; & cmd /c download_weights.bat; Pop-Location
    } elseif ((Test-Path $sh) -and (Get-Command bash -ErrorAction SilentlyContinue)) {
        Push-Location $MuseTalk; bash download_weights.sh; Pop-Location
    } else {
        Write-Host "  No download script runnable here. Follow the 'Download weights'" -ForegroundColor Yellow
        Write-Host "  section of $MuseTalk\README.md (HuggingFace: sd-vae, musetalk," -ForegroundColor Yellow
        Write-Host "  whisper, dwpose, face-parse-bisent), then re-run this script." -ForegroundColor Yellow
    }
}

Write-Host "[4/4] Configuration" -ForegroundColor Cyan
Write-Host @"

On THIS GPU PC, set these (user environment variables or the repo .env):
  MUSETALK_DIR=$MuseTalk
  MUSETALK_PYTHON=$Py
  MUSETALK_REF_VIDEO=C:\path\to\your-reference-clip.mp4   (2-3 min, you to camera)
  MUSETALK_WORKER_PORT=7862                                (7861 is SadTalker)
  AVATAR_OUT_DIR=C:\dev\agentic-rag\gpu_workers\output

Start the worker (own terminal, or register with NSSM as a service):
  `$env:MUSETALK_WORKER_PORT=7862; $Py -m gpu_workers.musetalk_worker

Health check (expect status: ok, engine: musetalk):
  curl http://localhost:7862/health

Then on the machine running the orchestrator, point the avatar tool at it:
  MEDIA_TOOL_MUSETALK_ENDPOINT=http://<this-pc-tailscale-ip>:7862
  MEDIA_TOOL_DEFAULT_AVATAR=musetalk

Verify from the orchestrator machine:
  python -m scripts.verify_media_providers
"@
