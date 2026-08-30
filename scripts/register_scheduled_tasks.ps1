# =============================================================================
# Registers the Windows Task Scheduler equivalents of the systemd timers in
# deploy/install.sh:
#   rag-watchdog.timer    -> every 5 minutes  -> scripts\watchdog.ps1
#   rag-rehearsal.timer   -> Mon 05:30        -> scripts\rehearsal.ps1
#   autostart on logon    -> Start RAG Ecosystem.bat (optional, -Autostart)
#
# THIS SCRIPT IS NOT RUN AUTOMATICALLY BY ANYTHING - it must be run
# deliberately, by hand, on the machine that should carry these tasks
# (wijerco for production). Registering these turns on standing background
# automation - including the operating daemon and, if configured, live
# Telegram/email channels - so don't run it until you're ready for that.
#
# Usage (run as the user who should own the tasks, elevated PowerShell not
# required - these are per-user scheduled tasks):
#   .\scripts\register_scheduled_tasks.ps1                # watchdog + rehearsal only
#   .\scripts\register_scheduled_tasks.ps1 -Autostart      # also register boot autostart
#   .\scripts\register_scheduled_tasks.ps1 -WhatIf         # show what would be created, change nothing
#
# Re-running is safe: existing tasks with the same names are replaced.
# =============================================================================

param(
    [switch]$Autostart,
    [switch]$WhatIf
)

$ProjectRoot = Split-Path $PSScriptRoot -Parent

function Register-OrPreview($taskName, $action, $trigger, $description) {
    if ($WhatIf) {
        Write-Host "[whatif] would register task '$taskName': $description" -ForegroundColor Yellow
        return
    }
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "[replace] existing task '$taskName' found - unregistering first" -ForegroundColor DarkYellow
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Description $description | Out-Null
    Write-Host "[ok] registered task '$taskName'" -ForegroundColor Green
}

# --- Watchdog: every 5 minutes, indefinitely ---------------------------------
$watchdogAction = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ProjectRoot\scripts\watchdog.ps1`""
$watchdogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration ([TimeSpan]::MaxValue)
Register-OrPreview "RAG Watchdog" $watchdogAction $watchdogTrigger `
    "Port liveness, daemon heartbeat, and deep health check every 5 minutes. Windows equivalent of deploy/watchdog.sh + rag-watchdog.timer."

# --- Rehearsal: every Monday 05:30 ------------------------------------------
$rehearsalAction = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ProjectRoot\scripts\rehearsal.ps1`""
$rehearsalTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "05:30"
Register-OrPreview "RAG Weekly Rehearsal" $rehearsalAction $rehearsalTrigger `
    "Weekly migrate/backup/restore-dry/rollback-dry rehearsal, Monday 05:30. Windows equivalent of deploy/rehearsal.sh + rag-rehearsal.timer."

# --- Optional: autostart on logon --------------------------------------------
if ($Autostart) {
    $autostartAction = New-ScheduledTaskAction -Execute "$ProjectRoot\Start RAG Ecosystem.bat" -WorkingDirectory $ProjectRoot
    $autostartTrigger = New-ScheduledTaskTrigger -AtLogOn
    Register-OrPreview "RAG Ecosystem Autostart" $autostartAction $autostartTrigger `
        "Runs Start RAG Ecosystem.bat at logon: Docker stack, core services, and (once wired in) the daemon/Telegram/email channels."
} else {
    Write-Host ""
    Write-Host "[note] Autostart-on-logon NOT registered (pass -Autostart to include it)." -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "Done. View with: Get-ScheduledTask | Where-Object { `$_.TaskName -like 'RAG*' }" -ForegroundColor Green
