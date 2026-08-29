<#
.SYNOPSIS
  Nightly backup of the agentic-rag-ecosystem brain: SQLite data, .env, cost log,
  and a snapshot of every Qdrant collection. Written for Stage 1 item 4 of the
  product finalisation plan (29 Aug 2026) - a single-node system with no tested
  backup is a prototype.

.PARAMETER RepoRoot
  Path to the agentic-rag repo (contains data/, logs/, .env, docker-compose.yml).

.PARAMETER BackupRoot
  Where finished backup archives land. Defaults to a OneDrive folder so backups
  get off this machine automatically via OneDrive sync (a cheap stand-in for
  "second machine or cloud storage" - point this at wijerco or real cloud
  storage instead if you'd rather not rely on OneDrive).

.PARAMETER KeepDays
  How many days of backups to retain locally before pruning. Default 14.

.PARAMETER QdrantUrl
  Base URL for the Qdrant HTTP API. Default matches .env's QDRANT_URL.
#>
param(
    [string]$RepoRoot = "C:\dev\agentic-rag-ecosystem",
    [string]$BackupRoot = "C:\Users\ajwij\OneDrive\Documents\Agents\agentic-rag-backups",
    [int]$KeepDays = 14,
    [string]$QdrantUrl = "http://localhost:6333"
)

$ErrorActionPreference = "Stop"
$ts = Get-Date -Format "yyyyMMdd-HHmmss"
$staging = Join-Path $env:TEMP "agentic-rag-backup-$ts"
$manifest = @{ timestamp = $ts; repo_root = $RepoRoot; files = @(); qdrant_collections = @() }

Write-Host "=== Backup $ts starting ==="
New-Item -ItemType Directory -Force -Path $staging | Out-Null

# 1. data/ (media.db incl. outcomes table, harness.db, sessions.db, evals.db,
#    state json, dashboard cache, harness_backups)
$dataSrc = Join-Path $RepoRoot "data"
$dataDst = Join-Path $staging "data"
if (Test-Path $dataSrc) {
    Copy-Item -Path $dataSrc -Destination $dataDst -Recurse -Force
    Get-ChildItem $dataDst -Recurse -File | ForEach-Object {
        $manifest.files += @{ path = "data\$($_.FullName.Substring($dataDst.Length+1))"; bytes = $_.Length }
    }
    Write-Host "OK  data/ -> $((Get-ChildItem $dataDst -Recurse -File | Measure-Object).Count) files"
} else {
    Write-Warning "MISSING data/ at $dataSrc"
}

# 2. logs/cost_log.jsonl
$costLog = Join-Path $RepoRoot "logs\cost_log.jsonl"
if (Test-Path $costLog) {
    New-Item -ItemType Directory -Force -Path (Join-Path $staging "logs") | Out-Null
    Copy-Item $costLog (Join-Path $staging "logs\cost_log.jsonl") -Force
    $manifest.files += @{ path = "logs\cost_log.jsonl"; bytes = (Get-Item $costLog).Length }
    Write-Host "OK  logs/cost_log.jsonl"
} else {
    Write-Warning "MISSING logs/cost_log.jsonl at $costLog"
}

# 3. .env (secrets - the zip below is not encrypted, treat the backup
#    destination itself as sensitive)
$envFile = Join-Path $RepoRoot ".env"
if (Test-Path $envFile) {
    Copy-Item $envFile (Join-Path $staging ".env") -Force
    $manifest.files += @{ path = ".env"; bytes = (Get-Item $envFile).Length }
    Write-Host "OK  .env"
} else {
    Write-Warning "MISSING .env at $envFile"
}

# 4. Qdrant: snapshot every collection, download the snapshot file
$qdrantDst = Join-Path $staging "qdrant_snapshots"
New-Item -ItemType Directory -Force -Path $qdrantDst | Out-Null
try {
    $collections = (Invoke-RestMethod -Uri "$QdrantUrl/collections" -Method Get).result.collections
    foreach ($c in $collections) {
        $name = $c.name
        try {
            $snap = Invoke-RestMethod -Uri "$QdrantUrl/collections/$name/snapshots" -Method Post
            $snapName = $snap.result.name
            $outDir = Join-Path $qdrantDst $name
            New-Item -ItemType Directory -Force -Path $outDir | Out-Null
            $outFile = Join-Path $outDir $snapName
            Invoke-WebRequest -Uri "$QdrantUrl/collections/$name/snapshots/$snapName" -OutFile $outFile -UseBasicParsing
            $bytes = (Get-Item $outFile).Length
            $manifest.qdrant_collections += @{ name = $name; snapshot = $snapName; bytes = $bytes }
            Write-Host "OK  qdrant/$name -> $snapName ($bytes bytes)"
            # clean up the snapshot on the server side once we have our copy
            Invoke-RestMethod -Uri "$QdrantUrl/collections/$name/snapshots/$snapName" -Method Delete | Out-Null
        } catch {
            Write-Warning "FAIL qdrant snapshot for $name : $($_.Exception.Message)"
        }
    }
} catch {
    Write-Warning "Qdrant unreachable at $QdrantUrl - skipping snapshots: $($_.Exception.Message)"
}

# 5. Zip it up and drop the staging copy
New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null
$manifest | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $staging "manifest.json")
$archive = Join-Path $BackupRoot "agentic-rag-backup-$ts.zip"
Compress-Archive -Path (Join-Path $staging "*") -DestinationPath $archive -Force
Remove-Item -Recurse -Force $staging

$archiveSize = (Get-Item $archive).Length
Write-Host "=== Backup complete: $archive ($([math]::Round($archiveSize/1MB,1)) MB) ==="

# 6. Prune backups older than KeepDays
$cutoff = (Get-Date).AddDays(-$KeepDays)
Get-ChildItem $BackupRoot -Filter "agentic-rag-backup-*.zip" | Where-Object { $_.LastWriteTime -lt $cutoff } | ForEach-Object {
    Write-Host "Pruning old backup: $($_.Name)"
    Remove-Item $_.FullName -Force
}

Write-Host "=== Done ==="
