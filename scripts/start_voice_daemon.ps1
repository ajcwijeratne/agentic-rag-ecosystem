# =============================================================================
# Always-on voice assistant.
#
# Deliberately NOT part of start_all.ps1: this process holds the microphone
# exclusively, so starting it would take the mic away from the Command Centre
# tab. Run whichever one you want to talk to, not both.
#
#   .\scripts\start_voice_daemon.ps1
#   .\scripts\start_voice_daemon.ps1 -Device 2 -Voice "Catherine"
#   .\scripts\start_voice_daemon.ps1 -ListDevices
# =============================================================================
param(
    [string]$Device = "",
    [string]$Voice = "",
    [switch]$ListDevices,
    [switch]$NoWake
)

$ProjectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $ProjectRoot
$Py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if ($ListDevices) { & $Py -m media.voice_daemon --list-devices; exit }

$argv = @("-m", "media.voice_daemon")
if ($Device) { $argv += @("--device", $Device) }
if ($Voice)  { $argv += @("--voice", $Voice) }
if ($NoWake) { $argv += "--no-wake" }

Write-Host "[voice] starting always-on assistant (Ctrl+C to stop)" -ForegroundColor Cyan
& $Py @argv
