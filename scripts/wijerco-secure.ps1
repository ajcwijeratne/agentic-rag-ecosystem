# ============================================================================
#  wijerco-secure.ps1  -  v2, 5 Sep 2026
#
#  Run this ON WIJERCO, via wijerco-secure.bat (double-click).
#
#  Does four things. Every step is independent: a step that fails reports why
#  and the next one still runs. v1 set $ErrorActionPreference='Stop' and then
#  called python with 2>&1, which throws a terminating error the moment python
#  writes to stderr. The import check in step 1 was always going to do that
#  while the orchestrator is broken, so v1 died at step 1 and did nothing.
#
#    1. Says why port 8000 is down, and does not guess.
#    2. Moves any plaintext remote_deploy_credentials.json out of the repo.
#    3. Rotates the n8n deploy webhook secret. The old one was published on a
#       public GitHub repo between 29 Aug and 4 Sep 2026.
#    4. Gives the orchestrator three separate RBAC keys instead of one, then
#       restarts and proves the three roles actually separate.
#
#  Safe to re-run. It backs up .env before touching it and restores that backup
#  if the orchestrator will not come up afterwards.
#
#  SECRETS ARE NEVER WRITTEN TO THE LOG. They go to one file in your user
#  folder, named at the end. Move them to your password manager, delete it.
# ============================================================================

$ErrorActionPreference = 'Continue'      # deliberate. see the note above.
$ProgressPreference    = 'SilentlyContinue'

$stamp   = Get-Date -Format 'yyyyMMdd-HHmmss'
$repo    = if (Test-Path 'C:\dev\agentic-rag-ecosystem\docker-compose.yml') { 'C:\dev\agentic-rag-ecosystem' } else { 'C:\dev\agentic-rag' }
$log     = Join-Path $env:USERPROFILE "wijerco_secure_log_$stamp.txt"
$secrets = Join-Path $env:USERPROFILE "wijerco-secrets-$stamp.txt"
$quar    = Join-Path 'C:\dev' "_quarantine\$stamp"
$tailIP  = '100.109.75.69'
$lines   = New-Object Collections.ArrayList

function Say([string]$m) { [void]$lines.Add($m); Write-Host $m }

# Runs a native command without letting stderr become a terminating error.
function Native([string]$cmdline) {
    try { $o = cmd /c "$cmdline 2>&1"; if ($null -eq $o) { @() } else { @($o) } }
    catch { @("could not run: $($_.Exception.Message)") }
}

# Runs a step. A failure inside it is reported, never fatal.
function Step([string]$title, [scriptblock]$body) {
    Say ""
    Say $title
    try { & $body } catch { Say ("  STEP FAILED: {0}" -f $_.Exception.Message); Say "  Continuing to the next step." }
}

function NewKey {
    $b = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    [Convert]::ToBase64String($b).TrimEnd('=').Replace('+','-').Replace('/','_')
}

Say "wijerco secure v2 - $(Get-Date -Format o)"
Say "Repo: $repo"
Say "Host: $env:COMPUTERNAME   PowerShell: $($PSVersionTable.PSVersion)"

# --- 1. Why is 8000 down? ----------------------------------------------------
Step "[1/4] Orchestrator on port 8000" {
    $listening = @(Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)
    if ($listening.Count -gt 0) { Say "  Port 8000 is listening. Nothing to diagnose."; return }

    Say "  Port 8000 is NOT listening."
    $py = @(Get-Process python, pythonw -ErrorAction SilentlyContinue)
    Say ("  python processes running: {0}" -f $py.Count)
    foreach ($p in $py) { Say ("    pid {0}  started {1}" -f $p.Id, $p.StartTime) }

    if (-not (Test-Path (Join-Path $repo '.venv\Scripts\python.exe'))) {
        Say "  NO VIRTUALENV at $repo\.venv. That alone would stop every start script."
    } else {
        Say "  Import check (what the update script tests before restarting):"
        foreach ($l in (Native "cd /d ""$repo"" && .venv\Scripts\python.exe -c ""import orchestrator.main; print('imports OK')""")) { Say "    $l" }
    }

    $logDir = Join-Path $repo 'logs'
    if (Test-Path $logDir) {
        $recent = Get-ChildItem $logDir -Filter *.log -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 3
        if (-not $recent) { Say "  No .log files in $logDir" }
        foreach ($f in $recent) {
            Say ""
            Say ("  --- last 25 lines of {0}  (modified {1}) ---" -f $f.Name, $f.LastWriteTime)
            foreach ($l in (Get-Content $f.FullName -Tail 25 -ErrorAction SilentlyContinue)) { Say "    $l" }
        }
    } else { Say "  No logs directory at $logDir" }
}

# --- 2. Plaintext credential -------------------------------------------------
Step "[2/4] Plaintext deploy credential" {
    $found = @(Get-ChildItem -Path $repo -Recurse -Filter 'remote_deploy_credentials.json' -ErrorAction SilentlyContinue)
    if ($found.Count -eq 0) { Say "  None found in the repo. Good."; return }
    New-Item -ItemType Directory -Force -Path $quar -ErrorAction SilentlyContinue | Out-Null
    foreach ($f in $found) {
        Move-Item $f.FullName (Join-Path $quar $f.Name) -Force -ErrorAction SilentlyContinue
        Say ("  Moved out of the repo: {0}" -f $f.FullName)
    }
    Say "  Now in: $quar"
    Say "  Nothing was deleted. Delete that folder yourself once you are happy."
}

# --- 3. Rotate the n8n deploy webhook secret ---------------------------------
$script:newDeploy = NewKey
$script:rotated   = $false
Step "[3/4] Rotating the n8n deploy webhook secret" {
    $credPath = Join-Path $repo 'n8n\workflows\remote_deploy_credentials.json'
    try {
        New-Item -ItemType Directory -Force -Path (Split-Path $credPath) -ErrorAction SilentlyContinue | Out-Null
        $cred = @(@{ id='deploy-webhook-header-auth'; name='Deploy Webhook Secret'; type='httpHeaderAuth'; data=@{ name='x-deploy-secret'; value=$script:newDeploy } })
        [IO.File]::WriteAllText($credPath, ($cred | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))

        foreach ($l in (Native "cd /d ""$repo"" && docker compose exec -T n8n n8n import:credentials --input=/home/node/.n8n/workflows/remote_deploy_credentials.json")) { Say "    $l" }
        $probe = Native "cd /d ""$repo"" && docker compose ps --status running --services"
        if ($probe -contains 'n8n') {
            foreach ($l in (Native "cd /d ""$repo"" && docker compose restart n8n")) { Say "    $l" }
            $script:rotated = $true
            Say "  Imported and n8n restarted. Verify below."
        } else {
            Say "  n8n does not appear to be running under docker compose here."
            Say "  Nothing was rotated. The old secret is still live."
        }
    } finally {
        if (Test-Path $credPath) { Remove-Item $credPath -Force -ErrorAction SilentlyContinue; Say "  Temporary credential file removed from the repo." }
    }
}

# --- 4. RBAC keys ------------------------------------------------------------
$script:viewer = NewKey; $script:operator = NewKey; $script:admin = NewKey
$script:backup = ""
Step "[4/4] RBAC keys" {
    $envPath = Join-Path $repo '.env'
    if (-not (Test-Path $envPath)) { Say "  No .env at $envPath. Skipping this step."; return }

    $script:backup = "$envPath.bak-$stamp"
    Copy-Item $envPath $script:backup -Force
    Say "  .env backed up to $($script:backup)"

    $json = "{`"viewer`":`"$($script:viewer)`",`"operator`":`"$($script:operator)`",`"admin`":`"$($script:admin)`"}"
    $src  = [IO.File]::ReadAllLines($envPath)
    $done = $false
    $new  = foreach ($l in $src) { if ($l -match '^\s*RBAC_ROLE_KEYS\s*=') { $done = $true; "RBAC_ROLE_KEYS=$json" } else { $l } }
    if (-not $done) { $new = @($new) + @("", "# Three separate keys so viewer, operator and admin actually differ.", "RBAC_ROLE_KEYS=$json") }
    [IO.File]::WriteAllLines($envPath, $new, (New-Object Text.UTF8Encoding($false)))
    Say ("  RBAC_ROLE_KEYS written with three distinct keys (replaced an existing line: {0})." -f $done)

    $starter = Join-Path $repo 'scripts\start_all.ps1'
    if (-not (Test-Path $starter)) { Say "  No scripts\start_all.ps1 here. Not restarting; keys are written but not live."; return }

    Say "  Restarting the core services ..."
    foreach ($l in (Native "powershell -NoProfile -ExecutionPolicy Bypass -File ""$starter""")) { Say "    $l" }
    Start-Sleep -Seconds 25

    function Code([string]$url, [string]$key) {
        try {
            $h = @{}; if ($key) { $h['x-api-key'] = $key }
            (Invoke-WebRequest -Uri $url -Headers $h -TimeoutSec 10 -UseBasicParsing -ErrorAction Stop).StatusCode
        } catch { if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { -1 } }
    }

    Say ""
    $health = Code "http://localhost:8000/health" $null
    Say ("    /health (local)              -> {0}   want 200" -f $health)
    if ($health -ne 200) {
        Say ""
        Say "  ORCHESTRATOR DID NOT COME UP."
        Copy-Item $script:backup $envPath -Force
        Say "  .env restored from $($script:backup). The machine is no worse than before."
        Say "  Step 1 above is the place to look."
        return
    }
    Say "  Verifying over the tailnet address, because loopback is always admin:"
    $checks = @(
        @{ n='no key     -> /ops/status '; c=(Code "http://${tailIP}:8000/ops/status"  $null);            w='401 or 403' },
        @{ n='viewer     -> /ops/status '; c=(Code "http://${tailIP}:8000/ops/status"  $script:viewer);   w='200' },
        @{ n='viewer     -> /ops/backups'; c=(Code "http://${tailIP}:8000/ops/backups" $script:viewer);   w='403' },
        @{ n='operator   -> /ops/backups'; c=(Code "http://${tailIP}:8000/ops/backups" $script:operator); w='200' },
        @{ n='admin      -> /ops/backups'; c=(Code "http://${tailIP}:8000/ops/backups" $script:admin);    w='200' }
    )
    foreach ($x in $checks) { Say ("    {0} -> {1}   want {2}" -f $x.n, $x.c, $x.w) }
    Say "  A 200 for the viewer key on /ops/backups means the roles are NOT separating."
}

# --- verify the rotation actually took ---------------------------------------
Step "[extra] Deploy webhook state" {
    $u = "http://${tailIP}:5678/webhook/deploy-file"
    $c = try { (Invoke-WebRequest -Uri $u -Method POST -Body '{}' -ContentType 'application/json' -TimeoutSec 10 -UseBasicParsing -ErrorAction Stop).StatusCode }
         catch { if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { -1 } }
    Say ("  POST $u with no auth -> {0}" -f $c)
    Say "  403 means the webhook is live and demanding the header."
    Say "  404 means that workflow is not active in n8n at all, so nothing can call it."
}

# --- secrets file, log, and getting the log back -----------------------------
[IO.File]::WriteAllLines($secrets, @(
    "WijerCo secrets generated $(Get-Date -Format o) on $env:COMPUTERNAME.",
    "Put these in your password manager, then delete this file.",
    "",
    "n8n deploy webhook  x-deploy-secret : $($script:newDeploy)   (rotated: $($script:rotated))",
    "",
    "RBAC_ROLE_KEYS",
    "  viewer   : $($script:viewer)",
    "  operator : $($script:operator)",
    "  admin    : $($script:admin)",
    "",
    "The cockpit over the tailnet needs the operator key. Loopback on this",
    "machine is always admin and needs no key.",
    "",
    "Previous .env: $($script:backup)"
), (New-Object Text.UTF8Encoding($false)))

Say ""
Say "============================================================"
Say " Finished $(Get-Date -Format o)"
Say " Secrets (not in this log): $secrets"
Say "============================================================"

[IO.File]::WriteAllLines($log, $lines, (New-Object Text.UTF8Encoding($false)))

# Three ways home, because Taildrop alone failed last time.
$delivered = @()
# Best path home: this exact folder is a connected folder on wijwork, so a file
# landing here syncs straight to where Aaron's session can read it. Taildrop has
# never actually delivered a log from this machine, so it is the fallback now.
if ($env:OneDrive -and (Test-Path $env:OneDrive)) {
    $d = Join-Path $env:OneDrive "Documents\Agents\agentic-rag-ecosystem\_logs"
    New-Item -ItemType Directory -Force -Path $d -ErrorAction SilentlyContinue | Out-Null
    $dest = Join-Path $d (Split-Path $log -Leaf)
    Copy-Item $log $dest -Force -ErrorAction SilentlyContinue
    if (Test-Path $dest) { $delivered += "OneDrive (syncs to wijwork): $dest" }
    else { $delivered += "OneDrive copy failed" }
} else { $delivered += "No OneDrive on this machine" }
$ts = @('C:\Program Files\Tailscale\tailscale.exe','C:\Program Files (x86)\Tailscale\tailscale.exe') | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($ts) { $out = Native "\"$ts\" file cp \"$log\" wijwork:"; if ($LASTEXITCODE -eq 0) { $delivered += "Taildrop to wijwork" } else { $delivered += "Taildrop FAILED: $($out -join ' ')" } }
else { $delivered += "Taildrop skipped: tailscale.exe not found" }

Write-Host ""
Write-Host "Log written to: $log"
foreach ($d in $delivered) { Write-Host "  also: $d" }
Write-Host ""
Write-Host "If none of those reached Aaron's other machine, the whole log is printed"
Write-Host "above and can be copied straight out of this window."
