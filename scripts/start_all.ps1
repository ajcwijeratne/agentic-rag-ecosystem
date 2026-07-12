# =============================================================================
# Start all Python services (Windows)
# Run from project root after activating .venv:
#   .\.venv\Scripts\Activate.ps1
#   .\scripts\start_all.ps1
# =============================================================================

$ProjectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $ProjectRoot

# Always launch services with the project's venv interpreter, never a bare
# `python` from PATH (which resolves to the base/system Python and is missing
# the venv site-packages -> ModuleNotFoundError: fastapi/uvicorn/apprise).
$Py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Host "[fatal] venv interpreter not found at $Py" -ForegroundColor Red
    Write-Host "        Create it with:  python -m venv .venv ; .\.venv\Scripts\python.exe -m pip install -r requirements.txt" -ForegroundColor Yellow
    exit 1
}

# Load .env into current session
if (Test-Path ".env") {
    Get-Content ".env" | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
            $key = $matches[1].Trim()
            $val = $matches[2].Trim().Trim('"').Trim("'")
            [System.Environment]::SetEnvironmentVariable($key, $val, "Process")
        }
    }
}

New-Item -ItemType Directory -Force -Path "logs" | Out-Null

# ---------------------------------------------------------------------------
# Free our ports from any stale instance left by a previous run.
# Without this, a leftover process holds the port, the new one fails to bind
# (Errno 10048) and dies silently, leaving old code running.
# ---------------------------------------------------------------------------
$ports = 8000,8001,8002,8003,8004,8005,8006
foreach ($p in $ports) {
    try {
        $owners = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue |
                  Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($procId in $owners) {
            if ($procId) {
                Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
                Write-Host "[clean] Freed port $p (PID $procId)" -ForegroundColor DarkYellow
            }
        }
    } catch {}
}
# Clear stale PID files
Get-ChildItem "logs\*.pid" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
Start-Sleep 2

function Start-Service($name, $module) {
    $logFile = "logs\$name.log"
    $pidFile = "logs\$name.pid"
    Write-Host "[start] $name -> $logFile" -ForegroundColor Cyan

    $proc = Start-Process $Py `
        -ArgumentList "-m $module" `
        -RedirectStandardOutput $logFile `
        -RedirectStandardError  "logs\$name.err" `
        -WorkingDirectory $ProjectRoot `
        -PassThru -WindowStyle Hidden

    $proc.Id | Out-File $pidFile -Encoding ASCII
}

Start-Service "orchestrator"    "orchestrator.main"
Start-Sleep 1
Start-Service "local_data_agent" "agents.local_data_agent"
Start-Service "search_agent"     "agents.search_agent"
Start-Service "cloud_agent"      "agents.cloud_agent"
Start-Service "indexer"          "rag.indexer --serve"
Start-Service "retriever"        "rag.retriever"
Start-Service "notifier"         "notifications.notifier --serve"

# Media services (uncomment to autostart)
# Start-Service "whisper"  "media.whisper_pipeline --serve"
# Start-Service "video"    "media.video_pipeline --serve"

Write-Host ""
Write-Host "All services started. Logs in .\logs\" -ForegroundColor Green
Write-Host "  Orchestrator API  -> http://localhost:8000/docs" -ForegroundColor White
Write-Host "  Command Centre    -> Open ui\command_centre.html in browser" -ForegroundColor White
