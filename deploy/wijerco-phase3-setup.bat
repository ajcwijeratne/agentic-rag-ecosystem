@echo off
REM ============================================================
REM  wijerco Phase 3 setup v1 - 26 Nov 2026
REM  Run on wijerco AFTER wijerco-update.bat and Stop/Start.
REM  Not as administrator. It:
REM   1. checks the brain is serving the Phase 3 page
REM   2. installs pywebpush (phone alerts) into the brain's Python
REM   3. checks the alert service is ready (makes its keys once)
REM  No restart needed: the library loads when first used.
REM  Log: wijerco_phase3_setup_log.txt next to this file.
REM ============================================================
set "P3LOG=%~dp0wijerco_phase3_setup_log.txt"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=Get-Content -Raw -LiteralPath '%~f0'; $s=$s.Substring($s.IndexOf('#PS'+'START')); Invoke-Expression $s"
echo.
echo Finished. Tell Claude "done".
pause
goto :eof

#PSSTART
$ErrorActionPreference = 'Stop'
$LOG = $env:P3LOG
Set-Content -Path $LOG -Value ''
function say($m) { $l = "[{0}] {1}" -f (Get-Date -f 'HH:mm:ss'), $m; Write-Host $l; Add-Content -Path $LOG -Value $l }
function run($cmdline, [int]$tail = 0) {
  $out = @(cmd /c "$cmdline 2>&1")
  if ($tail -gt 0 -and $out.Count -gt $tail) { $out = $out[-$tail..-1] }
  foreach ($x in $out) { say "   $x" }
  return $LASTEXITCODE
}
try {
  say "wijerco Phase 3 setup v1 on $env:COMPUTERNAME"
  $repo = 'C:\dev\agentic-rag-ecosystem'
  $py = "$repo\.venv\Scripts\python.exe"
  $o = 'http://127.0.0.1:8000'
  Set-Location $repo
  say ('   code: ' + ((cmd /c 'git log -1 --oneline') | Select-Object -First 1))

  say 'STEP 1  Page and service worker'
  $page = (Invoke-WebRequest "$o/app/command_centre.html" -UseBasicParsing -TimeoutSec 30).Content
  $sw = (Invoke-WebRequest "$o/app/sw.js" -UseBasicParsing -TimeoutSec 30).Content
  say ("   five-job page: {0} | alerts in service worker: {1}" -f ($page -match 'function JobTabs'), ($sw -match 'cc-shell-v12'))
  if (-not ($page -match 'function JobTabs')) { throw 'the brain is not serving the Phase 3 page yet. Run wijerco-update.bat, then Stop and Start, then this file again.' }

  say 'STEP 2  pywebpush'
  if ((run "$py -m pip install --disable-pip-version-check -q pywebpush" 5) -ne 0) { throw 'pip install failed' }

  say 'STEP 3  Alert service'
  $ps = Invoke-RestMethod "$o/push/status" -TimeoutSec 30
  say ("   library={0} devices={1} key ready={2}" -f $ps.library, $ps.devices, [bool]$ps.public_key)
  if (-not $ps.library) { throw 'pywebpush did not load' }
  say 'ALL DONE. On your phone: open the Command Centre, then System, Connections, Turn on alerts on this device.'
} catch {
  say "FAILED: $_"
}
