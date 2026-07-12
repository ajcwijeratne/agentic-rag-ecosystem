# Stop all Python services by reading PID files
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$LogDir = Join-Path $ProjectRoot "logs"

Get-ChildItem "$LogDir\*.pid" | ForEach-Object {
    $name   = $_.BaseName
    $procId = Get-Content $_.FullName -ErrorAction SilentlyContinue
    if ($procId) {
        try {
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
            Write-Host "[stop] $name (PID $procId)" -ForegroundColor Yellow
        } catch {
            Write-Host "[stop] $name already stopped" -ForegroundColor DarkGray
        }
        Remove-Item $_.FullName -Force
    }
}
Write-Host "All services stopped." -ForegroundColor Green
