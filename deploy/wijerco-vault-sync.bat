@echo off
REM ============================================================
REM  wijerco vault sync setup v1 - 24 Sep 2026
REM  Double-click on wijerco. It:
REM   1. installs Syncthing to C:\dev\syncthing (starts at logon)
REM   2. pairs with wijwork and receives the Obsidian vault
REM      into C:\dev\ObsidianVault (two-way from then on)
REM   3. waits for the first sync (up to 30 min)
REM   4. points OBSIDIAN_VAULT_PATH in .env at it (backs up .env)
REM   5. re-indexes the vault into Qdrant and restarts services
REM   6. Taildrops its log back to wijwork
REM  Safe to re-run. If Windows Firewall asks, click Allow.
REM ============================================================
set "VSLOG=%~dp0wijerco_vault_sync_log.txt"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=Get-Content -Raw -LiteralPath '%~f0'; $s=$s.Substring($s.IndexOf('#PS'+'START')); Invoke-Expression $s"
echo.
echo Sending the log back to wijwork ...
"C:\Program Files\Tailscale\tailscale.exe" file cp "%VSLOG%" wijwork:
echo.
echo Finished. You can close this window.
pause
goto :eof

#PSSTART
$ErrorActionPreference = 'Stop'
$LOG = $env:VSLOG
Set-Content -Path $LOG -Value ''
function say($m) { $l = "[{0}] {1}" -f (Get-Date -f 'HH:mm:ss'), $m; Write-Host $l; Add-Content -Path $LOG -Value $l }
function run($cmdline) { $out = cmd /c "$cmdline 2>&1"; foreach ($x in $out) { say "   $x" }; return $LASTEXITCODE }

try {
  say "wijerco vault sync setup v1 on $env:COMPUTERNAME"
  $WIJWORK = 'KXFRVOM-LNZ6XHY-U3RWBYT-Z63PSX7-MGVITKD-TEZVLND-I66ZHZZ-3BT6DQE'
  $d = 'C:\dev\syncthing'; $vault = 'C:\dev\ObsidianVault'; $exe = "$d\syncthing.exe"
  if (Test-Path 'C:\dev\agentic-rag-ecosystem\docker-compose.yml') { $repo = 'C:\dev\agentic-rag-ecosystem' } else { $repo = 'C:\dev\agentic-rag' }
  say "repo: $repo"
  New-Item -ItemType Directory -Force $d, "$d\home", $vault | Out-Null

  say 'STEP 1  Syncthing install'
  if (-not (Test-Path $exe)) {
    $zip = "$d\st.zip"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest 'https://github.com/syncthing/syncthing/releases/download/v2.1.5/syncthing-windows-amd64-v2.1.5.zip' -OutFile $zip -UseBasicParsing
    Expand-Archive $zip "$d\x" -Force
    Copy-Item (Get-ChildItem "$d\x" -Recurse -Filter syncthing.exe | Select-Object -First 1).FullName $exe -Force
    Remove-Item $zip, "$d\x" -Recurse -Force
  }
  run "$exe --version" | Out-Null
  if (-not (Test-Path "$d\home\config.xml")) { run "$exe generate --home $d\home --no-port-probing" | Out-Null }
  Set-Content "$d\run-hidden.vbs" -Encoding ASCII -Value @'
Set sh = CreateObject("WScript.Shell")
sh.Environment("PROCESS")("STNODEFAULTFOLDER") = "1"
sh.Run """C:\dev\syncthing\syncthing.exe"" serve --home ""C:\dev\syncthing\home"" --no-browser --gui-address=127.0.0.1:8384 --log-file=""C:\dev\syncthing\syncthing.log""", 0, False
'@
  $a = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument '"C:\dev\syncthing\run-hidden.vbs"'
  $t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $st = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  Register-ScheduledTask -TaskName 'Syncthing' -Action $a -Trigger $t -Settings $st -Force | Out-Null
  if (-not (Get-Process syncthing -ErrorAction SilentlyContinue)) { Start-Process wscript.exe '"C:\dev\syncthing\run-hidden.vbs"' }
  say '   scheduled task Syncthing registered (starts at logon)'

  say 'STEP 2  Pair with wijwork'
  [xml]$c = Get-Content "$d\home\config.xml"
  $h = @{ 'X-API-Key' = $c.configuration.gui.apikey }; $b = 'http://127.0.0.1:8384/rest'
  $me = $null
  for ($i = 0; $i -lt 60 -and -not $me; $i++) { try { $me = (Invoke-RestMethod "$b/system/status" -Headers $h).myID } catch { Start-Sleep 3 } }
  if (-not $me) { throw 'Syncthing did not start within 3 minutes' }
  say "   wijerco device ID: $me"
  $devs = Invoke-RestMethod "$b/config/devices" -Headers $h
  if (-not ($devs | Where-Object { $_.deviceID -eq $WIJWORK })) {
    $dev = Invoke-RestMethod "$b/config/defaults/device" -Headers $h
    $dev.deviceID = $WIJWORK; $dev.name = 'wijwork'
    $dev.addresses = @('tcp://100.64.137.16:22000', 'quic://100.64.137.16:22000', 'dynamic')
    Invoke-RestMethod "$b/config/devices" -Method Post -Headers $h -ContentType 'application/json' -Body ($dev | ConvertTo-Json -Depth 10) | Out-Null
    say '   added wijwork'
  }
  $fid = 'obsidian-vault'
  $folders = Invoke-RestMethod "$b/config/folders" -Headers $h
  if (-not ($folders | Where-Object { $_.id -eq $fid })) {
    $f = Invoke-RestMethod "$b/config/defaults/folder" -Headers $h
    $f.id = $fid; $f.label = 'Obsidian Vault'; $f.path = $vault; $f.type = 'sendreceive'; $f.fsWatcherEnabled = $true
    $f.versioning = @{ type = 'staggered'; params = @{ maxAge = '2592000'; cleanInterval = '3600' }; cleanupIntervalS = 3600; fsPath = ''; fsType = 'basic' }
    $f.devices = @(@{ deviceID = $me }, @{ deviceID = $WIJWORK })
    Invoke-RestMethod "$b/config/folders" -Method Post -Headers $h -ContentType 'application/json' -Body ($f | ConvertTo-Json -Depth 10) | Out-Null
    say "   added folder $fid at $vault"
    Start-Sleep 3
  }
  $ign = @('.obsidian/workspace.json', '.obsidian/workspace-mobile.json', '.obsidian/cache', '.trash', '(?d)desktop.ini', '(?d).DS_Store', '(?d)~$*')
  Invoke-RestMethod "$b/db/ignores?folder=$fid" -Method Post -Headers $h -ContentType 'application/json' -Body (@{ ignore = $ign } | ConvertTo-Json) | Out-Null

  say 'STEP 3  First sync (wijwork accepts automatically; up to 30 min)'
  $done = $false; $end = (Get-Date).AddMinutes(30); $last = Get-Date '2000-01-01'
  while ((Get-Date) -lt $end) {
    $conn = $false; try { $conn = (Invoke-RestMethod "$b/system/connections" -Headers $h).connections.$WIJWORK.connected } catch {}
    $s = Invoke-RestMethod "$b/db/status?folder=$fid" -Headers $h
    if (((Get-Date) - $last).TotalSeconds -ge 60) { say ("   connected={0} state={1} files {2}/{3} need={4}" -f $conn, $s.state, $s.localFiles, $s.globalFiles, $s.needFiles); $last = Get-Date }
    if ($conn -and $s.globalFiles -gt 100 -and $s.needFiles -eq 0 -and $s.state -eq 'idle') { $done = $true; break }
    Start-Sleep 10
  }
  if (-not $done) { throw 'first sync did not finish in 30 min. Syncthing keeps going in the background; re-run this file later.' }
  $n = (Get-ChildItem $vault -Recurse -File -Filter *.md | Measure-Object).Count
  say "   vault synced: $n markdown files in $vault"

  say 'STEP 4  Point .env at the local vault'
  $envf = Join-Path $repo '.env'
  $lines = Get-Content $envf
  $old = $lines | Where-Object { $_ -match '^\s*OBSIDIAN_VAULT_PATH=' }
  say "   before: $old"
  $new = 'OBSIDIAN_VAULT_PATH=C:/dev/ObsidianVault'
  if ($old -and ($old -join '') -eq $new) { say '   already set' } else {
    Copy-Item $envf ("$envf.bak-vaultsync-" + (Get-Date -f 'yyyyMMdd-HHmmss'))
    if ($old) { $lines = $lines | ForEach-Object { if ($_ -match '^\s*OBSIDIAN_VAULT_PATH=') { $new } else { $_ } } } else { $lines = @($lines) + $new }
    [IO.File]::WriteAllLines($envf, [string[]]$lines, (New-Object Text.UTF8Encoding $false))
    say "   after:  $new  (backup written)"
  }

  say 'STEP 5  Re-index the vault into Qdrant'
  Push-Location $repo
  $rc = run ".venv\Scripts\python.exe -m rag.indexer --vault C:/dev/ObsidianVault"
  say "   indexer exit code $rc"
  say 'STEP 6  Restart services so they read the new path'
  $rc2 = run "powershell -NoProfile -ExecutionPolicy Bypass -File $repo\scripts\start_all.ps1"
  say "   start_all exit code $rc2"
  Pop-Location
  say 'ALL DONE'
} catch {
  say "FAILED: $_"
}
