# =============================================================================
# Watchdog (Windows). Meant to be run every 5 minutes by a Task Scheduler
# task - see scripts/register_scheduled_tasks.ps1. Windows port of
# deploy/watchdog.sh (the Linux/systemd version), same three checks:
#
#   1. Port liveness for every core service; two consecutive failures
#      restart that one service (re-launched the same way
#      scripts/start_all.ps1 / scripts/start_channels.ps1 launch it).
#   2. Daemon heartbeat freshness; stale beyond 10 minutes restarts the
#      daemon. Only checked if logs\daemon.pid exists, i.e. the daemon is
#      actually meant to be running on this box.
#   3. Deep health on the orchestrator; a "down" dependency is logged and
#      notified after two strikes (Docker services have their own restart
#      policy via docker-compose.yml, so this step never restarts them).
#
# Failure counters live under $env:TEMP\rag-watchdog so one blip never
# restarts anything; two in a row does. Safe to run manually: `.\scripts\
# watchdog.ps1` does exactly what the scheduled task does, once.
# =============================================================================

$ProjectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $ProjectRoot

$Py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$StateDir = Join-Path $env:TEMP "rag-watchdog"
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
New-Item -ItemType Directory -Force -Path "logs" | Out-Null

function Write-Log($msg) {
    $line = "$(Get-Date -Format o) $msg"
    Add-Content -Path "logs\watchdog.log" -Value $line
}

function Send-Notice($body) {
    try {
        Invoke-RestMethod -Uri "http://localhost:8004/notify" -Method Post `
            -ContentType "application/json" `
            -Body (@{ title = "Watchdog"; body = $body } | ConvertTo-Json) `
            -TimeoutSec 10 | Out-Null
    } catch { }
}

# Keep this map in sync with scripts/start_all.ps1's Start-Service calls and
# scripts/start_channels.ps1's Start-ChannelService calls - it's how the
# watchdog knows what module to relaunch for a given service name.
$ServiceModules = @{
    "orchestrator"     = "orchestrator.main"
    "local_data_agent" = "agents.local_data_agent"
    "search_agent"     = "agents.search_agent"
    "cloud_agent"      = "agents.cloud_agent"
    "notifier"         = "notifications.notifier --serve"
    "indexer"          = "rag.indexer --serve"
    "retriever"        = "rag.retriever"
    "daemon"           = "orchestrator.daemon"
}

function Restart-ManagedService($name, $reason) {
    Write-Log "restarting $name`: $reason"
    $pidFile = "logs\$name.pid"
    if (Test-Path $pidFile) {
        $oldId = Get-Content $pidFile -ErrorAction SilentlyContinue
        if ($oldId) { Stop-Process -Id $oldId -Force -ErrorAction SilentlyContinue }
    }
    $module = $ServiceModules[$name]
    $proc = Start-Process $Py `
        -ArgumentList "-m $module" `
        -RedirectStandardOutput "logs\$name.log" `
        -RedirectStandardError  "logs\$name.err" `
        -WorkingDirectory $ProjectRoot `
        -PassThru -WindowStyle Hidden
    $proc.Id | Out-File $pidFile -Encoding ASCII
    Send-Notice "Restarted $name ($reason)"
}

# --- 1. Port liveness, two strikes ------------------------------------------
$Ports = @{
    "orchestrator"     = 8000
    "local_data_agent" = 8001
    "search_agent"     = 8002
    "cloud_agent"      = 8003
    "notifier"         = 8004
    "indexer"          = 8005
    "retriever"        = 8006
}

foreach ($name in $Ports.Keys) {
    $pidFile = "logs\$name.pid"
    # Only police services this launcher is actually meant to be running -
    # mirrors watchdog.sh's `systemctl is-enabled` guard.
    if (-not (Test-Path $pidFile)) { continue }

    $port = $Ports[$name]
    $counterFile = Join-Path $StateDir "$name.fails"
    $up = $false
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:$port/health" -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
        $up = $true
    } catch {
        try {
            $r = Invoke-WebRequest -Uri "http://localhost:$port/" -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
            $up = $true
        } catch { $up = $false }
    }

    if ($up) {
        Remove-Item $counterFile -ErrorAction SilentlyContinue
    } else {
        $fails = 1
        if (Test-Path $counterFile) { $fails = [int](Get-Content $counterFile) + 1 }
        Set-Content -Path $counterFile -Value $fails
        Write-Log "$name port $port unresponsive (strike $fails)"
        if ($fails -ge 2) {
            Restart-ManagedService $name "port $port down twice"
            Remove-Item $counterFile -ErrorAction SilentlyContinue
        }
    }
}

# --- 2. Daemon heartbeat -----------------------------------------------------
$hbPath = "logs\daemon_heartbeat"
if (Test-Path "logs\daemon.pid") {
    if (Test-Path $hbPath) {
        $age = (Get-Date) - (Get-Item $hbPath).LastWriteTime
        if ($age.TotalSeconds -gt 600) {
            Restart-ManagedService "daemon" "heartbeat stale $([int]$age.TotalSeconds)s"
        }
    }
}

# --- 3. Deep health -----------------------------------------------------------
try {
    $deep = Invoke-RestMethod -Uri "http://localhost:8000/health/deep" -TimeoutSec 15 -ErrorAction Stop
    $deepJson = $deep | ConvertTo-Json -Depth 6 -Compress
} catch {
    $deepJson = ""
}

$deepCounter = Join-Path $StateDir "deep.fails"
if ($deepJson -and $deepJson -match '"down"') {
    $fails = 1
    if (Test-Path $deepCounter) { $fails = [int](Get-Content $deepCounter) + 1 }
    Set-Content -Path $deepCounter -Value $fails
    Write-Log "deep health reports a down dependency (strike $fails)"
    if ($fails -ge 2) {
        Send-Notice "Deep health check reports a down dependency. Check /health/deep."
        Remove-Item $deepCounter -ErrorAction SilentlyContinue
    }
} else {
    Remove-Item $deepCounter -ErrorAction SilentlyContinue
}
