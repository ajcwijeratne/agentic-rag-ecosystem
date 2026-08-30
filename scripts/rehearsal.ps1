# =============================================================================
# Weekly operational rehearsal (Windows). Meant to be run every Monday
# morning by a Task Scheduler task - see scripts/register_scheduled_tasks.ps1.
# Windows port of deploy/rehearsal.sh (the Linux/systemd version). Follows
# the same flow as docs/hardening-operational-rehearsal.md:
#   migrate -> backup -> restore (dry run) -> release snapshot ->
#   rollback (dry run) -> monitoring -> rehearsal verdict
#
# The result lands in logs\rehearsal.log and a summary goes through the
# notifier, same as the Linux version. Exits non-zero when attention is
# needed. Safe to run by hand: .\scripts\rehearsal.ps1
#
# Uses ADMIN_API_KEY (falling back to API_KEY) from .env for the /ops/*
# calls, matching common/rbac.py's role resolution. The key is only ever
# passed as a request header, never written to the log - only the response
# body is logged.
#
# NOTE: /ops/restore and /ops/releases/rollback both require a "path" field
# (FastAPI 422s without one) - the upstream deploy/rehearsal.sh omits it and
# would 422 on those two steps too if actually run. This port fixes that by
# feeding each dry-run step the path just produced by its own backup/snapshot
# step, so restore-dry rehearses against a real backup and rollback-dry
# against a real snapshot, both still fully no-op because dry_run=true short-
# circuits before either function touches a file.
# =============================================================================

$ProjectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $ProjectRoot

$Base = if ($env:ORCHESTRATOR_URL) { $env:ORCHESTRATOR_URL } else { "http://localhost:8000" }
$Log = "logs\rehearsal.log"
New-Item -ItemType Directory -Force -Path "logs" | Out-Null

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

$apiKey = if ($env:ADMIN_API_KEY) { $env:ADMIN_API_KEY } elseif ($env:API_KEY) { $env:API_KEY } else { "" }
$headers = @{ "Content-Type" = "application/json" }
if ($apiKey) { $headers["X-API-Key"] = $apiKey }

function Write-RLog($msg) {
    $line = "$(Get-Date -Format o) $msg"
    Add-Content -Path $Log -Value $line
    Write-Host $line
}

# Returns the parsed response object (not just its JSON text) so callers can
# thread a value (e.g. a backup's .path) into a later step.
function Invoke-Step($name, $method, $path, $body) {
    $uri = "$Base$path"
    $result = $null
    try {
        if ($body) {
            $result = Invoke-RestMethod -Uri $uri -Method $method -Headers $headers -Body $body -TimeoutSec 120 -ErrorAction Stop
        } else {
            $result = Invoke-RestMethod -Uri $uri -Method $method -Headers $headers -TimeoutSec 120 -ErrorAction Stop
        }
        $out = ($result | ConvertTo-Json -Depth 6 -Compress)
    } catch {
        $out = "ERROR: $($_.Exception.Message)"
    }
    $trimmed = if ($out.Length -gt 400) { $out.Substring(0, 400) } else { $out }
    Write-RLog "[$name] $trimmed"
    return $result
}

function Send-RehearsalNotice($body) {
    try {
        Invoke-RestMethod -Uri "http://localhost:8004/notify" -Method Post `
            -ContentType "application/json" `
            -Body (@{ title = "Weekly rehearsal"; body = $body } | ConvertTo-Json) `
            -TimeoutSec 10 | Out-Null
    } catch { }
}

Write-RLog "=== rehearsal start ==="

Invoke-Step "migrate" "POST" "/ops/migrate" $null | Out-Null

$backupResult = Invoke-Step "backup" "POST" "/ops/backup" $null
$backupPath = if ($backupResult -and $backupResult.path) { $backupResult.path } else { $null }
if ($backupPath) {
    $restoreBody = (@{ dry_run = $true; path = $backupPath } | ConvertTo-Json)
    Invoke-Step "restore-dry" "POST" "/ops/restore" $restoreBody | Out-Null
} else {
    Write-RLog "[restore-dry] skipped: no backup path returned by /ops/backup"
}

$snapshotResult = Invoke-Step "snapshot" "POST" "/ops/releases/snapshot" (@{ note = "weekly rehearsal" } | ConvertTo-Json)
$snapshotPath = if ($snapshotResult -and $snapshotResult.path) { $snapshotResult.path } else { $null }
if ($snapshotPath) {
    $rollbackBody = (@{ dry_run = $true; path = $snapshotPath } | ConvertTo-Json)
    Invoke-Step "rollback-dry" "POST" "/ops/releases/rollback" $rollbackBody | Out-Null
} else {
    Write-RLog "[rollback-dry] skipped: no snapshot path returned by /ops/releases/snapshot"
}

Invoke-Step "monitoring" "GET" "/ops/monitoring" $null | Out-Null

$verdictResult = Invoke-Step "rehearsal" "GET" "/ops/rehearsal" $null
$verdictJson = if ($verdictResult) { ($verdictResult | ConvertTo-Json -Depth 6 -Compress) } else { "" }

if ($verdictJson -match "needs_attention") {
    Write-RLog "=== rehearsal verdict: NEEDS ATTENTION ==="
    Send-RehearsalNotice "needs_attention. Check /ops/rehearsal and logs\rehearsal.log."
    exit 1
}

Write-RLog "=== rehearsal verdict: pass ==="
Send-RehearsalNotice "Pass. Migrate, backup, restore dry run, snapshot, rollback dry run all clean."
exit 0
