@echo off
REM ============================================================
REM  wijerco backups + WijerCo knowledge base setup v1 - 22 Nov 2026
REM  Double-click on wijerco (not as administrator). It:
REM   1. adds two Syncthing folders shared with wijwork:
REM        wijerco-knowledge  C:\dev\WijerCo          two-way
REM        wijerco-backups    C:\dev\wijerco-backups  send only
REM   2. waits for the WijerCo folder to arrive (up to 15 min)
REM   3. sets WIJERCO_PATH and KB_REINDEX_TARGETS in .env (backs it up)
REM   4. registers the scheduled task "WijerCo Nightly Backup" (2:30am)
REM   5. copies router_labels.jsonl (if it sits next to this file) to data\
REM   6. runs one backup now and checks the zip was written
REM   7. Taildrops its log back to wijwork
REM  Then run Stop RAG Ecosystem, then Start RAG Ecosystem.
REM  Safe to re-run.
REM ============================================================
set "BKLOG=%~dp0wijerco_backup_kb_setup_log.txt"
set "BKDIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=Get-Content -Raw -LiteralPath '%~f0'; $s=$s.Substring($s.IndexOf('#PS'+'START')); Invoke-Expression $s"
echo.
echo Sending the log back to wijwork ...
"C:\Program Files\Tailscale\tailscale.exe" file cp "%BKLOG%" wijwork:
echo.
echo Finished. Next: Stop RAG Ecosystem, then Start RAG Ecosystem.
pause
goto :eof

#PSSTART
$ErrorActionPreference = 'Stop'
$LOG = $env:BKLOG
Set-Content -Path $LOG -Value ''
function say($m) { $l = "[{0}] {1}" -f (Get-Date -f 'HH:mm:ss'), $m; Write-Host $l; Add-Content -Path $LOG -Value $l }
function run($cmdline) { $out = cmd /c "$cmdline 2>&1"; foreach ($x in $out) { say "   $x" }; return $LASTEXITCODE }

try {
  say "wijerco backup + knowledge base setup v1 on $env:COMPUTERNAME"
  $repo = if (Test-Path 'C:\dev\agentic-rag-ecosystem\docker-compose.yml') { 'C:\dev\agentic-rag-ecosystem' } else { 'C:\dev\agentic-rag' }
  $WIJWORK = 'KXFRVOM-LNZ6XHY-U3RWBYT-Z63PSX7-MGVITKD-TEZVLND-I66ZHZZ-3BT6DQE'
  $kb = 'C:\dev\WijerCo'; $bk = 'C:\dev\wijerco-backups'
  New-Item -ItemType Directory -Force $kb, $bk | Out-Null

  say 'STEP 1  Syncthing folders'
  [xml]$c = Get-Content 'C:\dev\syncthing\home\config.xml'
  $h = @{ 'X-API-Key' = $c.configuration.gui.apikey }; $b = 'http://127.0.0.1:8384/rest'
  $me = (Invoke-RestMethod "$b/system/status" -Headers $h).myID
  say "   this device: $me"
  $existing = Invoke-RestMethod "$b/config/folders" -Headers $h
  foreach ($spec in @(
      @{ id = 'wijerco-knowledge'; label = 'WijerCo knowledge'; path = $kb; type = 'sendreceive' },
      @{ id = 'wijerco-backups';   label = 'wijerco backups';   path = $bk; type = 'sendonly' })) {
    if ($existing | Where-Object { $_.id -eq $spec.id }) { say "   $($spec.id) already present"; continue }
    $f = Invoke-RestMethod "$b/config/defaults/folder" -Headers $h
    $f.id = $spec.id; $f.label = $spec.label; $f.path = $spec.path; $f.type = $spec.type; $f.fsWatcherEnabled = $true
    if ($spec.type -eq 'sendreceive') {
      $f.versioning = @{ type = 'staggered'; params = @{ maxAge = '2592000'; cleanInterval = '3600' }; cleanupIntervalS = 3600; fsPath = ''; fsType = 'basic' }
    }
    $f.devices = @(@{ deviceID = $me }, @{ deviceID = $WIJWORK })
    Invoke-RestMethod "$b/config/folders" -Method Post -Headers $h -ContentType 'application/json' -Body ($f | ConvertTo-Json -Depth 10) | Out-Null
    say "   added $($spec.id) at $($spec.path) ($($spec.type))"
    Start-Sleep 3
  }
  $ign = @('(?d)desktop.ini', '(?d).DS_Store', '(?d)~$*', '(?d)__pycache__', '(?d)*.pyc', '(?d)*.tmp')
  Invoke-RestMethod "$b/db/ignores?folder=wijerco-knowledge" -Method Post -Headers $h -ContentType 'application/json' -Body (@{ ignore = $ign } | ConvertTo-Json) | Out-Null

  say 'STEP 2  Waiting for the WijerCo folder (up to 15 min)'
  $end = (Get-Date).AddMinutes(15); $last = Get-Date '2000-01-01'; $ok = $false
  while ((Get-Date) -lt $end) {
    try { $s = Invoke-RestMethod "$b/db/status?folder=wijerco-knowledge" -Headers $h } catch { Start-Sleep 5; continue }
    if (((Get-Date) - $last).TotalSeconds -ge 30) { say ("   state={0} files {1}/{2} need={3}" -f $s.state, $s.localFiles, $s.globalFiles, $s.needFiles); $last = Get-Date }
    if ($s.globalFiles -gt 50 -and $s.needFiles -eq 0 -and $s.state -eq 'idle') { $ok = $true; break }
    Start-Sleep 10
  }
  $kbFiles = @(Get-ChildItem "$kb\KNOWLEDGE BASE" -Filter *.md -ErrorAction SilentlyContinue).Count
  if ($ok) { say "   synced; KNOWLEDGE BASE has $kbFiles notes" } else { say "   not finished yet (KNOWLEDGE BASE has $kbFiles notes); Syncthing carries on in the background" }

  say 'STEP 3  .env'
  $envf = Join-Path $repo '.env'
  Copy-Item $envf ("$envf.bak-backupkb-" + (Get-Date -f 'yyyyMMdd-HHmmss'))
  $lines = [Collections.Generic.List[string]]@(Get-Content $envf)
  $want = [ordered]@{ WIJERCO_PATH = 'C:/dev/WijerCo'; KB_REINDEX_TARGETS = '/index,/index/wijerco' }
  foreach ($k in $want.Keys) {
    $i = -1; for ($j = 0; $j -lt $lines.Count; $j++) { if ($lines[$j] -match "^\s*$k\s*=") { $i = $j; break } }
    if ($i -ge 0) { $lines[$i] = "$k=$($want[$k])" } else { $lines.Add("$k=$($want[$k])") }
    say "   $k=$($want[$k])"
  }
  [IO.File]::WriteAllLines($envf, [string[]]$lines, (New-Object Text.UTF8Encoding $false))

  say 'STEP 4  Scheduled task "WijerCo Nightly Backup" (2:30am)'
  $vbs = 'C:\dev\wijerco-backup-run.vbs'
  # Paths have no spaces, so no inner quoting. Runs hidden; output in C:\dev\wijerco-backup-last.log.
  $ps = "powershell -NoProfile -ExecutionPolicy Bypass -File $repo\deploy\backup.ps1 -RepoRoot $repo -BackupRoot $bk -KeepDays 14"
  Set-Content -Path $vbs -Encoding ASCII -Value ('CreateObject("WScript.Shell").Run "cmd /c ' + $ps + ' > C:\dev\wijerco-backup-last.log 2>&1", 0, True')
  $act = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('"' + $vbs + '"')
  $trg = New-ScheduledTaskTrigger -Daily -At '2:30am'
  $set = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)
  Register-ScheduledTask -TaskName 'WijerCo Nightly Backup' -Action $act -Trigger $trg -Settings $set -Force | Out-Null
  say '   registered'

  say 'STEP 5  Routing labels'
  $src = Join-Path $env:BKDIR 'router_labels.jsonl'; $dst = Join-Path $repo 'data\router_labels.jsonl'
  if (Test-Path $dst) { say "   already present at $dst (left as is)" }
  elseif (Test-Path $src) { Copy-Item $src $dst; say ("   copied {0} labelled queries to {1}" -f @(Get-Content $dst).Count, $dst) }
  else { say '   router_labels.jsonl not next to this file; skipped' }

  say 'STEP 6  One backup now (a few minutes)'
  $rc = run "powershell -NoProfile -ExecutionPolicy Bypass -File $repo\deploy\backup.ps1 -RepoRoot $repo -BackupRoot $bk -KeepDays 14"
  say "   backup exit code $rc"
  $z = Get-ChildItem $bk -Filter 'agentic-rag-backup-*.zip' | Sort-Object LastWriteTime | Select-Object -Last 1
  if ($z) { say ("   newest backup: {0} ({1} MB)" -f $z.Name, [math]::Round($z.Length / 1MB, 1)) } else { say '   NO BACKUP FILE FOUND' }
  say 'ALL DONE'
} catch {
  say "FAILED: $_"
}
