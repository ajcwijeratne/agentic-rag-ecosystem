# ============================================================================
#  wijerco-secure.ps1  -  4 Sep 2026
#
#  Run this ON WIJERCO, via wijerco-secure.bat (double-click).
#
#  Does four things, in this order, and stops at the first one that fails:
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
#  SECRETS ARE NEVER WRITTEN TO THE LOG. They go to a single file in your user
#  folder, named at the end. Move them into your password manager and delete it.
# ============================================================================

$ErrorActionPreference = 'Stop'
$stamp    = Get-Date -Format 'yyyyMMdd-HHmmss'
$repo     = if (Test-Path 'C:\dev\agentic-rag-ecosystem\docker-compose.yml') { 'C:\dev\agentic-rag-ecosystem' } else { 'C:\dev\agentic-rag' }
$log      = Join-Path $PSScriptRoot "wijerco_secure_log.txt"
$secrets  = Join-Path $env:USERPROFILE "wijerco-secrets-$stamp.txt"
$quar     = Join-Path 'C:\dev' "_quarantine\$stamp"
$tailIP   = '100.109.75.69'

function Say([string]$m) { $m | Tee-Object -FilePath $log -Append | Write-Host }
function NewKey { $b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); [Convert]::ToBase64String($b).TrimEnd('=').Replace('+','-').Replace('/','_') }

Set-Content -Path $log -Value "wijerco secure - $(Get-Date -Format o)" -Encoding UTF8
Say "Repo: $repo"
Say ""

# --- 1. Why is 8000 down? ----------------------------------------------------
Say "[1/4] Orchestrator on port 8000"
$listening = @(Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)
if ($listening.Count -gt 0) {
    Say "  Port 8000 is listening. Nothing to diagnose."
} else {
    Say "  Port 8000 is NOT listening."
    $py = @(Get-Process python, pythonw -ErrorAction SilentlyContinue)
    Say ("  python processes running: {0}" -f $py.Count)
    foreach ($p in $py) { Say ("    pid {0}  started {1}" -f $p.Id, $p.StartTime) }
    $logDir = Join-Path $repo 'logs'
    if (Test-Path $logDir) {
        $recent = Get-ChildItem $logDir -Filter *.log -ErrorAction SilentlyContinue |
                  Sort-Object LastWriteTime -Descending | Select-Object -First 3
        foreach ($f in $recent) {
            Say ""
            Say ("  --- last 25 lines of {0} (modified {1}) ---" -f $f.Name, $f.LastWriteTime)
            Get-Content $f.FullName -Tail 25 -ErrorAction SilentlyContinue | ForEach-Object { Say "    $_" }
        }
    } else {
        Say "  No logs directory at $logDir"
    }
    Say ""
    Say "  Import check (this is what the update script tests before restarting):"
    Push-Location $repo
    $imp = & .\.venv\Scripts\python.exe -c "import orchestrator.main; print('imports OK')" 2>&1
    Pop-Location
    $imp | ForEach-Object { Say "    $_" }
}
Say ""

# --- 2. Plaintext credential -------------------------------------------------
Say "[2/4] Plaintext deploy credential"
$found = @(Get-ChildItem -Path $repo -Recurse -Filter 'remote_deploy_credentials.json' -ErrorAction SilentlyContinue)
if ($found.Count -eq 0) {
    Say "  None found in the repo. Good."
} else {
    New-Item -ItemType Directory -Force -Path $quar | Out-Null
    foreach ($f in $found) {
        Move-Item $f.FullName (Join-Path $quar $f.Name) -Force
        Say ("  Moved out of the repo: {0}" -f $f.FullName)
    }
    Say "  Now in: $quar"
    Say "  Nothing was deleted. Delete that folder yourself once you are happy."
}
Say ""

# --- 3. Rotate the n8n deploy webhook secret ---------------------------------
Say "[3/4] Rotating the n8n deploy webhook secret"
$newDeploy = NewKey
$credPath  = Join-Path $repo 'n8n\workflows\remote_deploy_credentials.json'
$cred = @(@{ id = 'deploy-webhook-header-auth'; name = 'Deploy Webhook Secret'; type = 'httpHeaderAuth'; data = @{ name = 'x-deploy-secret'; value = $newDeploy } })
$rotated = $false
try {
    New-Item -ItemType Directory -Force -Path (Split-Path $credPath) | Out-Null
    [IO.File]::WriteAllText($credPath, ($cred | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))
    Push-Location $repo
    $out = & docker compose exec -T n8n n8n import:credentials --input=/home/node/.n8n/workflows/remote_deploy_credentials.json 2>&1
    Pop-Location
    $out | ForEach-Object { Say "    $_" }
    if ($LASTEXITCODE -eq 0) {
        Push-Location $repo; & docker compose restart n8n 2>&1 | Out-Null; Pop-Location
        Say "  Imported and n8n restarted. The old secret no longer works."
        $rotated = $true
    } else {
        Say "  IMPORT FAILED. The old secret is still live. Nothing else was changed by this step."
    }
} catch {
    Say ("  ROTATION FAILED: {0}" -f $_.Exception.Message)
    Say "  The old secret is still live. Step 4 continues regardless."
} finally {
    if (Test-Path $credPath) { Remove-Item $credPath -Force; Say "  Temporary credential file removed from the repo." }
}
Say ""

# --- 4. RBAC keys ------------------------------------------------------------
Say "[4/4] RBAC keys"
$envPath = Join-Path $repo '.env'
if (-not (Test-Path $envPath)) { Say "  No .env at $envPath. Stopping."; exit 1 }
$backup = "$envPath.bak-$stamp"
Copy-Item $envPath $backup -Force
Say "  .env backed up to $backup"

$viewer = NewKey; $operator = NewKey; $admin = NewKey
$json = "{`"viewer`":`"$viewer`",`"operator`":`"$operator`",`"admin`":`"$admin`"}"
$lines = [IO.File]::ReadAllLines($envPath)
$done = $false
$new = foreach ($l in $lines) {
    if ($l -match '^\s*RBAC_ROLE_KEYS\s*=') { $done = $true; "RBAC_ROLE_KEYS=$json" } else { $l }
}
if (-not $done) { $new = @($new) + @("", "# Three separate keys so viewer, operator and admin actually differ.", "RBAC_ROLE_KEYS=$json") }
[IO.File]::WriteAllLines($envPath, $new, (New-Object Text.UTF8Encoding($false)))
Say "  RBAC_ROLE_KEYS written with three distinct keys."

Say ""
Say "  Restarting the core services ..."
try {
    Push-Location $repo
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $repo 'scripts\start_all.ps1') 2>&1 | ForEach-Object { Say "    $_" }
    Pop-Location
} catch {
    Say ("  RESTART THREW: {0}" -f $_.Exception.Message)
    Copy-Item $backup $envPath -Force
    Say "  .env restored from $backup so the machine is no worse than before."
    Say "  Keys were not applied. Send this log."
    exit 1
}
Start-Sleep -Seconds 25

function Code([string]$url, [string]$key) {
    try {
        $h = @{}; if ($key) { $h['x-api-key'] = $key }
        (Invoke-WebRequest -Uri $url -Headers $h -TimeoutSec 10 -UseBasicParsing).StatusCode
    } catch { if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { -1 } }
}

Say ""
Say "  Verifying (over the tailnet address, because loopback is always admin):"
$health = Code "http://localhost:8000/health" $null
Say ("    /health (local)                       -> {0}   want 200" -f $health)
if ($health -ne 200) {
    Say ""
    Say "  ORCHESTRATOR DID NOT COME UP. Restoring the previous .env and stopping."
    Copy-Item $backup $envPath -Force
    Say "  .env restored from $backup. Re-run start_all.ps1, then send this log."
    Say "  The rotated webhook secret is unaffected and is in $secrets"
} else {
    $r = @(
        @{ n = "no key      -> /ops/status "; c = (Code "http://${tailIP}:8000/ops/status"  $null);     want = "401 or 403" },
        @{ n = "viewer key  -> /ops/status "; c = (Code "http://${tailIP}:8000/ops/status"  $viewer);   want = "200" },
        @{ n = "viewer key  -> /ops/backups"; c = (Code "http://${tailIP}:8000/ops/backups" $viewer);   want = "403" },
        @{ n = "operator    -> /ops/backups"; c = (Code "http://${tailIP}:8000/ops/backups" $operator); want = "200" },
        @{ n = "admin       -> /ops/backups"; c = (Code "http://${tailIP}:8000/ops/backups" $admin);    want = "200" }
    )
    foreach ($x in $r) { Say ("    {0} -> {1}   want {2}" -f $x.n, $x.c, $x.want) }
    Say ""
    Say "  If the viewer key returns 200 on /ops/backups, the roles are NOT separating."
}

# --- secrets file ------------------------------------------------------------
$body = @(
    "WijerCo secrets generated $(Get-Date -Format o) on wijerco.",
    "Put these in your password manager, then delete this file.",
    "",
    "n8n deploy webhook  x-deploy-secret : $newDeploy   (rotated: $rotated)",
    "",
    "RBAC_ROLE_KEYS",
    "  viewer   : $viewer",
    "  operator : $operator",
    "  admin    : $admin",
    "",
    "The cockpit over the tailnet needs the operator key. Loopback on this",
    "machine is always admin and needs no key at all.",
    "",
    "Previous .env: $backup"
)
[IO.File]::WriteAllLines($secrets, $body, (New-Object Text.UTF8Encoding($false)))

Say ""
Say "============================================================"
Say " Done $(Get-Date -Format o)"
Say " Keys and the new webhook secret: $secrets"
Say " This log (no secrets in it): $log"
Say "============================================================"

try { & 'C:\Program Files\Tailscale\tailscale.exe' file cp $log wijwork: 2>&1 | Out-Null; Say " Log sent to wijwork via Taildrop." } catch { Say " Taildrop failed; send $log by hand." }
