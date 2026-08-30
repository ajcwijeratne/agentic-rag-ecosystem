<#
.SYNOPSIS
  Restore drill: takes a backup archive produced by backup.ps1, restores it into
  a throwaway location, and verifies every piece is actually usable - not just
  present. This is the "tested restore" half of Stage 1 item 4; a backup nobody
  has restored from is not a backup, it's a hope.

.PARAMETER Archive
  Path to the backup zip. Defaults to the newest one in the standard backup folder.

.PARAMETER QdrantUrl
  Base URL for the Qdrant HTTP API, used to test-restore one snapshot into a
  throwaway collection and confirm the point count matches, then delete it.

.PARAMETER RepoRoot
  Path to the agentic-rag repo, used only to derive the default PythonExe
  below. Auto-detects between wijwork's clone (C:\dev\agentic-rag) and
  wijerco's clone (C:\dev\agentic-rag-ecosystem) so this script runs
  unmodified on either machine; pass -RepoRoot or -PythonExe explicitly
  to override.

.PARAMETER PythonExe
  Python interpreter with sqlite3 (stdlib - any python3 works) for the DB
  integrity checks. Defaults to the venv under RepoRoot.
#>
param(
    [string]$Archive = "",
    [string]$BackupRoot = "C:\Users\ajwij\OneDrive\Documents\Agents\agentic-rag-backups",
    [string]$QdrantUrl = "http://localhost:6333",
    [string]$RepoRoot = $(if (Test-Path "C:\dev\agentic-rag-ecosystem\docker-compose.yml") { "C:\dev\agentic-rag-ecosystem" } else { "C:\dev\agentic-rag" }),
    [string]$PythonExe = "$RepoRoot\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$results = @()

if (-not $Archive) {
    $latest = Get-ChildItem $BackupRoot -Filter "agentic-rag-backup-*.zip" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $latest) { throw "No backup archives found in $BackupRoot" }
    $Archive = $latest.FullName
}
Write-Host "=== Restore drill against: $Archive ==="
$startTime = Get-Date

$restoreDir = Join-Path $env:TEMP "agentic-rag-restore-drill-$(Get-Date -Format yyyyMMdd-HHmmss)"
New-Item -ItemType Directory -Force -Path $restoreDir | Out-Null
Expand-Archive -Path $Archive -DestinationPath $restoreDir -Force
Write-Host "Extracted to $restoreDir"

# 1. manifest present and parses
$manifestPath = Join-Path $restoreDir "manifest.json"
if (Test-Path $manifestPath) {
    $manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
    $results += [pscustomobject]@{ check = "manifest.json readable"; ok = $true; detail = "timestamp $($manifest.timestamp)" }
} else {
    $results += [pscustomobject]@{ check = "manifest.json readable"; ok = $false; detail = "missing" }
}

# 2. every SQLite file opens and passes integrity_check
$sqliteCheckPy = Join-Path $env:TEMP "sqlite_integrity_check_$($PID).py"
@'
import sqlite3, sys
path = sys.argv[1]
c = sqlite3.connect(path)
r = c.execute("PRAGMA integrity_check").fetchone()[0]
tables = [t[0] for t in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print(r + "|" + str(len(tables)) + "|" + ",".join(tables[:5]))
'@ | Set-Content -Path $sqliteCheckPy -Encoding UTF8

$dbFiles = Get-ChildItem (Join-Path $restoreDir "data") -Filter "*.db" -File -ErrorAction SilentlyContinue
foreach ($db in $dbFiles) {
    try {
        $out = & $PythonExe $sqliteCheckPy $db.FullName 2>&1
        $parts = ($out -join "") -split '\|'
        $ok = ($parts[0] -eq "ok")
        $results += [pscustomobject]@{ check = "sqlite: $($db.Name)"; ok = $ok; detail = "integrity=$($parts[0]) tables=$($parts[1]) sample=$($parts[2])" }
    } catch {
        $results += [pscustomobject]@{ check = "sqlite: $($db.Name)"; ok = $false; detail = $_.Exception.Message }
    }
}
Remove-Item $sqliteCheckPy -Force -ErrorAction SilentlyContinue

# 3. .env present and non-empty
$envPath = Join-Path $restoreDir ".env"
if ((Test-Path $envPath) -and (Get-Item $envPath).Length -gt 0) {
    $results += [pscustomobject]@{ check = ".env restorable"; ok = $true; detail = "$((Get-Item $envPath).Length) bytes" }
} else {
    $results += [pscustomobject]@{ check = ".env restorable"; ok = $false; detail = "missing or empty" }
}

# 4. cost log present
$costLogPath = Join-Path $restoreDir "logs\cost_log.jsonl"
$results += [pscustomobject]@{ check = "logs/cost_log.jsonl restorable"; ok = (Test-Path $costLogPath); detail = "$(if (Test-Path $costLogPath) { (Get-Item $costLogPath).Length } else { 0 }) bytes" }

# 5. Qdrant: actually restore one snapshot into a throwaway collection and check point count
$qdrantDir = Join-Path $restoreDir "qdrant_snapshots"
if (Test-Path $qdrantDir) {
    Get-ChildItem $qdrantDir -Directory | ForEach-Object {
        $collName = $_.Name
        $snapFile = Get-ChildItem $_.FullName -Filter "*.snapshot" | Select-Object -First 1
        if ($snapFile) {
            $testColl = "restore_drill_$collName"
            try {
                # Windows PowerShell 5.1 has no -Form on Invoke-RestMethod; curl.exe does multipart natively.
                $uploadUrl = "$QdrantUrl/collections/$testColl/snapshots/upload?priority=snapshot"
                $curlOut = & curl.exe -s -S -X POST $uploadUrl -F "snapshot=@$($snapFile.FullName)" 2>&1
                $curlResult = $curlOut | ConvertFrom-Json
                if ($curlResult.status -ne "ok") { throw "upload status: $($curlResult.status) - $curlOut" }
                $count = (Invoke-RestMethod -Uri "$QdrantUrl/collections/$testColl" -Method Get).result.points_count
                $results += [pscustomobject]@{ check = "qdrant restore: $collName"; ok = $true; detail = "$count points in throwaway collection $testColl" }
            } catch {
                $results += [pscustomobject]@{ check = "qdrant restore: $collName"; ok = $false; detail = $_.Exception.Message }
            } finally {
                try { Invoke-RestMethod -Uri "$QdrantUrl/collections/$testColl" -Method Delete | Out-Null } catch {}
            }
        }
    }
} else {
    $results += [pscustomobject]@{ check = "qdrant snapshots present"; ok = $false; detail = "no qdrant_snapshots dir in archive" }
}

$elapsed = (Get-Date) - $startTime
Remove-Item -Recurse -Force $restoreDir

Write-Host ""
Write-Host "=== Restore drill results ($([math]::Round($elapsed.TotalSeconds,1))s) ==="
$results | Format-Table -AutoSize
$failCount = ($results | Where-Object { -not $_.ok }).Count
if ($failCount -eq 0) {
    Write-Host "ALL CHECKS PASSED"
} else {
    Write-Host "$failCount CHECK(S) FAILED - see table above"
}
