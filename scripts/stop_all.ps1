# Stop all Python services by reading PID files, then by port.
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$LogDir = Join-Path $ProjectRoot "logs"
$failed = @()

Get-ChildItem "$LogDir\*.pid" | ForEach-Object {
    $name   = $_.BaseName
    $procId = Get-Content $_.FullName -ErrorAction SilentlyContinue
    if ($procId) {
        if (-not (Get-Process -Id $procId -ErrorAction SilentlyContinue)) {
            Write-Host "[stop] $name already stopped" -ForegroundColor DarkGray
        } else {
            try {
                Stop-Process -Id $procId -Force -ErrorAction Stop
                Write-Host "[stop] $name (PID $procId)" -ForegroundColor Yellow
            } catch {
                Write-Host "[stop] COULD NOT stop $name (PID $procId): $($_.Exception.Message)" -ForegroundColor Red
                $failed += $name
            }
        }
        Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
    }
}

# A PID file can name the venv launcher rather than the Python process that
# holds the port, so also stop whatever still listens on the service ports.
$ports = 8000,8001,8002,8003,8004,8005,8006,8009
foreach ($p in $ports) {
    $owners = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue |
              Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($procId in $owners) {
        if (-not $procId) { continue }
        try {
            Stop-Process -Id $procId -Force -ErrorAction Stop
            Write-Host "[stop] port $p (PID $procId)" -ForegroundColor Yellow
        } catch {
            Write-Host "[stop] COULD NOT stop port $p (PID $procId): $($_.Exception.Message)" -ForegroundColor Red
            $failed += "port $p"
        }
    }
}

if ($failed.Count -gt 0) {
    Write-Host ""
    Write-Host "NOT everything stopped: $($failed -join ', ')." -ForegroundColor Red
    Write-Host "These were probably started with administrator rights. Restart Windows to clear them." -ForegroundColor Red
} else {
    Write-Host "All services stopped." -ForegroundColor Green
}
