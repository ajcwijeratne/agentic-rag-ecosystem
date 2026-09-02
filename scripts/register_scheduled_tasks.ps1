# =============================================================================
# Registers the Windows Task Scheduler equivalents of the systemd timers in
# deploy/install.sh:
#   rag-watchdog.timer    -> every 5 minutes  -> scripts\watchdog.ps1
#   rag-rehearsal.timer   -> Mon 05:30        -> scripts\rehearsal.ps1
#   (new) weekly live eval -> Mon 05:00       -> scripts\weekly_live_eval.ps1
#   (new) nightly backup   -> daily 02:30     -> scripts\backup_system.py
#   autostart on logon    -> Start RAG Ecosystem.bat (optional, -Autostart)
#
# The live eval runs 30 minutes before the rehearsal task, not because they
# depend on each other, but so both weekly checks land before anyone's likely
# to be looking at the cockpit on a Monday morning, without racing the same
# Ollama instance against itself.
#
# THIS SCRIPT IS NOT RUN AUTOMATICALLY BY ANYTHING - it must be run
# deliberately, by hand, on the machine that should carry these tasks
# (wijerco for production). Registering these turns on standing background
# automation - including the operating daemon and, if configured, live
# Telegram/email channels - so don't run it until you're ready for that.
#
# Usage (run as the user who should own the tasks, elevated PowerShell not
# required - these are per-user scheduled tasks):
#   .\scripts\register_scheduled_tasks.ps1                # watchdog + rehearsal + live eval
#   .\scripts\register_scheduled_tasks.ps1 -Autostart      # also register boot autostart
#   .\scripts\register_scheduled_tasks.ps1 -WhatIf         # show what would be created, change nothing
#
# Re-running is safe: existing tasks with the same names are replaced.
# =============================================================================

param(
    [switch]$Autostart,
    [switch]$WhatIf,
    # Where nightly archives land. Point this at a location that does not
    # share a failure mode with C: (another disk, a NAS, or a synced folder)
    # or the backup dies with the thing it was meant to protect.
    [string]$BackupRoot = "C:\Backups\agentic-rag"
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

# --- Weekly live eval: golden + recall, every Monday 05:00 -------------------
$liveEvalAction = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ProjectRoot\scripts\weekly_live_eval.ps1`""
$liveEvalTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "05:00"
Register-OrPreview "RAG Weekly Live Eval" $liveEvalAction $liveEvalTrigger `
    "Golden + recall live retrieval eval, Monday 05:00, so a regression surfaces within days rather than at the next debugging session (Stage 1 item 15)."

# --- Nightly full backup: 02:30 ---------------------------------------------
# /ops/backup (which the Monday rehearsal calls) copies media.db alone into
# logs\db_backups, inside the repo on the same disk. This captures every
# database, every Qdrant collection, config and the cost ledger, and puts the
# archive somewhere the loss of C: does not take with it. Stage 1 item 4.
$backupAction = New-ScheduledTaskAction -Execute "$ProjectRoot\.venv\Scripts\python.exe" `
    -Argument "scripts\backup_system.py --keep 10 --roots `"$BackupRoot`"" -WorkingDirectory $ProjectRoot
$backupTrigger = New-ScheduledTaskTrigger -Daily -At "02:30"
Register-OrPreview "RAG Nightly Backup" $backupAction $backupTrigger `
    "Full-system backup (all SQLite DBs, Qdrant collections, config, cost ledger) to $BackupRoot, nightly at 02:30."

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
