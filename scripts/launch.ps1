# =============================================================================
# AGENTIC RAG ECOSYSTEM — One-click launcher (Windows)
# Boots the whole stack in order, then opens the Command Centre:
#   1. Docker Desktop (started if not already running)
#   2. Docker stack   : Qdrant, Ollama, n8n, SearXNG  (docker compose up -d)
#   3. Python services: orchestrator + agents + notifier (.venv)
#   4. Command Centre UI in the default browser
#
# Just double-click "Start RAG Ecosystem.bat" in the project root.
# =============================================================================

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $ProjectRoot

function Say($msg, $color = "White") { Write-Host $msg -ForegroundColor $color }

function Wait-ForUrl($url, $name, $timeoutSec = 90) {
    Say "[wait] $name ($url)" "DarkGray"
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
            if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500) {
                Say "[ok]   $name is up" "Green"; return $true
            }
        } catch { Start-Sleep -Seconds 2 }
    }
    Say "[warn] $name did not respond within ${timeoutSec}s (continuing)" "Yellow"
    return $false
}

function Get-EnvValue($key, $default) {
    $envFile = Join-Path $ProjectRoot ".env"
    if (Test-Path $envFile) {
        $line = Select-String -Path $envFile -Pattern "^\s*$key\s*=" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($line) {
            $v = ($line.Line -split '=', 2)[1]      # right of first '='
            $v = ($v -split '#', 2)[0].Trim().Trim('"').Trim("'")  # strip inline comment + quotes
            if ($v) { return $v }
        }
    }
    return $default
}

function Ensure-OllamaModel($model) {
    if (-not $model) { return }
    $present = docker exec ollama ollama list 2>$null | Select-String -SimpleMatch $model -Quiet
    if ($present) {
        Say "[ok]   model present: $model" "Green"
    } else {
        Say "[pull] model missing: $model - pulling now (first time only, may take a few minutes)..." "Yellow"
        docker exec ollama ollama pull $model
        if ($LASTEXITCODE -eq 0) { Say "[ok]   pulled: $model" "Green" }
        else { Say "[warn] pull failed for $model - check 'docker logs ollama'" "Yellow" }
    }
}

function Open-AppWindow($target) {
    $edge = @(
        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    $chrome = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1

    if ($edge)   { Start-Process $edge   -ArgumentList "--app=$target", "--window-size=1440,900"; return "Edge app window" }
    if ($chrome) { Start-Process $chrome -ArgumentList "--app=$target", "--window-size=1440,900"; return "Chrome app window" }
    Start-Process $target; return "default browser (Edge/Chrome not found for app mode)"
}

function Find-InstalledPwaShortcut([string[]]$names) {
    # When the PWA is installed, Edge/Chrome drop a Start-menu .lnk whose target
    # is the browser proxy with the right --app-id + profile. Launching that .lnk
    # opens the *installed* app (its own window identity), not a plain app window.
    $roots = @(
        (Join-Path $env:APPDATA   "Microsoft\Windows\Start Menu\Programs"),
        (Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs")
    ) | Where-Object { Test-Path $_ }

    foreach ($root in $roots) {
        foreach ($n in $names) {
            $hit = Get-ChildItem -Path $root -Recurse -Filter "$n.lnk" -ErrorAction SilentlyContinue |
                   Select-Object -First 1
            if ($hit) { return $hit.FullName }
        }
    }
    return $null
}

# ---------------------------------------------------------------------------
# 1. Docker Desktop
# ---------------------------------------------------------------------------
Say "`n=== 1/4  Docker Desktop ===" "Cyan"
$dockerReady = $false
try { docker info *> $null; if ($LASTEXITCODE -eq 0) { $dockerReady = $true } } catch {}

if (-not $dockerReady) {
    Say "[start] Docker Desktop is not running - launching it..." "Yellow"
    $dockerExe = @(
        "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe",
        "$env:LOCALAPPDATA\Docker\Docker Desktop.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1

    if ($dockerExe) {
        Start-Process $dockerExe | Out-Null
    } else {
        Say "[warn] Could not find Docker Desktop.exe - please start Docker manually." "Yellow"
    }

    Say "[wait] Waiting for Docker engine (this can take a minute on a cold start)..." "DarkGray"
    $deadline = (Get-Date).AddSeconds(180)
    while ((Get-Date) -lt $deadline) {
        try { docker info *> $null; if ($LASTEXITCODE -eq 0) { $dockerReady = $true; break } } catch {}
        Start-Sleep -Seconds 3
    }
}

if ($dockerReady) { Say "[ok]   Docker engine ready" "Green" }
else { Say "[fatal] Docker never became ready. Start Docker Desktop, then re-run." "Red"; Read-Host "Press Enter to close"; exit 1 }

# ---------------------------------------------------------------------------
# 2. Docker stack (Qdrant, Ollama, n8n, SearXNG)
# ---------------------------------------------------------------------------
Say "`n=== 2/4  Docker stack ===" "Cyan"
Say "[start] docker compose up -d" "Cyan"
docker compose up -d
if ($LASTEXITCODE -ne 0) {
    Say "[warn] 'docker compose' failed - trying legacy 'docker-compose'..." "Yellow"
    docker-compose up -d
}

# Wait for the services the Python agents depend on
Wait-ForUrl "http://localhost:6333/healthz"      "Qdrant"  | Out-Null
Wait-ForUrl "http://localhost:11434/api/tags"    "Ollama"  | Out-Null
Wait-ForUrl "http://localhost:5678"              "n8n"     | Out-Null
Wait-ForUrl "http://localhost:8080"              "SearXNG" | Out-Null

# ---------------------------------------------------------------------------
# 2b. Ollama models — pull the chat + embedding models if not already present.
#     Model names are read from .env so this stays correct if you change them.
# ---------------------------------------------------------------------------
Say "`n=== 2b/4  Ollama models ===" "Cyan"
$chatModel  = Get-EnvValue "OLLAMA_MODEL" "llama3"
$embedModel = Get-EnvValue "EMBED_MODEL"  "nomic-embed-text"
Ensure-OllamaModel $chatModel
Ensure-OllamaModel $embedModel

# ---------------------------------------------------------------------------
# 3. Python services (.venv)  — delegates to start_all.ps1
# ---------------------------------------------------------------------------
Say "`n=== 3/4  Python services ===" "Cyan"
& "$PSScriptRoot\start_all.ps1"

# Wait for the orchestrator API to answer before opening the UI
Wait-ForUrl "http://localhost:8000/docs" "Orchestrator API" | Out-Null

# ---------------------------------------------------------------------------
# 4. Command Centre UI
# ---------------------------------------------------------------------------
Say "`n=== 4/4  Command Centre ===" "Cyan"
# Served same-origin by the orchestrator so the PWA manifest + service worker
# load (file:// can't install as an app; http://localhost can).
$uiUrl = "http://localhost:8000/app/command_centre.html"
Wait-ForUrl $uiUrl "Command Centre page" 30 | Out-Null

# Prefer the *installed* PWA if it exists; otherwise open an app window and
# nudge the user to install it once.
$appNames = @("WijerCo Command Centre", "Command Centre")
$installed = Find-InstalledPwaShortcut $appNames
if ($installed) {
    Start-Process $installed
    Say "[open] Launched installed app: $([System.IO.Path]::GetFileNameWithoutExtension($installed))" "Green"
} else {
    $how = Open-AppWindow $uiUrl
    Say "[open] Command Centre in $how (not installed yet)" "Green"
    Say "       To make it a real app: in the window's '...' menu choose" "DarkGray"
    Say "       'Install Command Centre'. Next launch will open the installed app." "DarkGray"
}

Say "`nAll set. Service map:" "Green"
Say "  Command Centre   -> http://localhost:8000/app/command_centre.html (opened)" "White"
Say "  Orchestrator API -> http://localhost:8000/docs"      "White"
Say "  Qdrant dashboard -> http://localhost:6333/dashboard" "White"
Say "  n8n              -> http://localhost:5678"           "White"
Say "  SearXNG          -> http://localhost:8080"           "White"
Say "`nThis window can be closed - services keep running in the background." "DarkGray"
Start-Sleep -Seconds 6
