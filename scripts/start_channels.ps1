# =============================================================================
# Start the operating daemon + Telegram/email channels (Windows).
#
# Windows equivalent of the rag-daemon / rag-telegram / rag-email systemd
# units in deploy/install.sh (the Linux mini-PC path). Kept as a SEPARATE
# script from start_all.ps1 on purpose: start_all.ps1's own comments note the
# daemon and channel workers "are separate long-lived services and must
# retain their tracking files" - these are the always-on background pieces,
# distinct from the core API services.
#
# The daemon runs in whatever DAEMON_DRY_RUN says in .env (defaults to
# dry-run: it only logs planned actions, it does not execute them). Telegram
# and email only start when their credentials are present in .env - same
# skip logic as install.sh - so running this script on a box with neither
# configured is a harmless no-op beyond starting the daemon.
#
# Run from project root after start_all.ps1 (the daemon and channels talk to
# the orchestrator on localhost:8000):
#   .\scripts\start_channels.ps1
# =============================================================================

$ProjectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $ProjectRoot

$Py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Host "[fatal] venv interpreter not found at $Py" -ForegroundColor Red
    exit 1
}

$envFile = Join-Path $ProjectRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
            $key = $matches[1].Trim()
            $val = ($matches[2] -split '#', 2)[0].Trim().Trim('"').Trim("'")
            [System.Environment]::SetEnvironmentVariable($key, $val, "Process")
        }
    }
}

New-Item -ItemType Directory -Force -Path "logs" | Out-Null

function Start-ChannelService($name, $module) {
    $logFile = "logs\$name.log"
    $pidFile = "logs\$name.pid"

    # Don't double-start: if the PID file points at a still-running process,
    # leave it alone (this script is safe to re-run, e.g. from the watchdog).
    if (Test-Path $pidFile) {
        $existingId = Get-Content $pidFile -ErrorAction SilentlyContinue
        if ($existingId -and (Get-Process -Id $existingId -ErrorAction SilentlyContinue)) {
            Write-Host "[skip]  $name already running (PID $existingId)" -ForegroundColor DarkGray
            return
        }
    }

    Write-Host "[start] $name -> $logFile" -ForegroundColor Cyan
    $proc = Start-Process $Py `
        -ArgumentList "-m $module" `
        -RedirectStandardOutput $logFile `
        -RedirectStandardError  "logs\$name.err" `
        -WorkingDirectory $ProjectRoot `
        -PassThru -WindowStyle Hidden
    $proc.Id | Out-File $pidFile -Encoding ASCII
}

# --- Daemon: always started; DAEMON_DRY_RUN (default 1) gates whether it
#     actually executes plans or just logs what it would do. -----------------
Start-ChannelService "daemon" "orchestrator.daemon"

# --- Telegram: only if a bot token is configured (same check as install.sh) -
$hasTelegramToken = [bool](Select-String -Path $envFile -Pattern "^(TELEGRAM_BOT_TOKEN|APPRISE_TELEGRAM_TOKEN)=.+" -Quiet -ErrorAction SilentlyContinue)
if ($hasTelegramToken) {
    Start-ChannelService "telegram" "channels.telegram_bot"
} else {
    Write-Host "[skip]  telegram (no TELEGRAM_BOT_TOKEN / APPRISE_TELEGRAM_TOKEN in .env)" -ForegroundColor DarkYellow
}

# --- Email: only if allowed senders are configured (same check as install.sh)
$hasEmailSenders = [bool](Select-String -Path $envFile -Pattern "^EMAIL_ALLOWED_SENDERS=.+" -Quiet -ErrorAction SilentlyContinue)
if ($hasEmailSenders) {
    Start-ChannelService "email" "channels.email_poller"
} else {
    Write-Host "[skip]  email (no EMAIL_ALLOWED_SENDERS in .env)" -ForegroundColor DarkYellow
}

Write-Host ""
Write-Host "Channels started. Logs in .\logs\daemon.log, .\logs\telegram.log, .\logs\email.log" -ForegroundColor Green
