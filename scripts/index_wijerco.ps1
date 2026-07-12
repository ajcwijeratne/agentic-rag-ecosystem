# Index the WijerCo knowledge base into Qdrant
# Run once after first setup, then again whenever KNOWLEDGE BASE or AGENTS/ files change.
param(
    [string]$WijerCoPath = $env:WIJERCO_PATH
)

if (-not $WijerCoPath) {
    $WijerCoPath = "C:\Users\ajwij\Claude Cowork\WijerCo"
}

$IndexerUrl = if ($env:INDEXER_URL) { $env:INDEXER_URL } else { "http://localhost:8005" }

Write-Host "[index] Indexing WijerCo knowledge base: $WijerCoPath" -ForegroundColor Cyan
$body = @{ wijerco_path = $WijerCoPath } | ConvertTo-Json

try {
    $resp = Invoke-RestMethod "$IndexerUrl/index/wijerco" -Method Post -Body $body -ContentType "application/json"
    Write-Host "[index] Done." -ForegroundColor Green
    $resp | ConvertTo-Json -Depth 3
} catch {
    Write-Host "[error] $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "        Is the indexer running? Check: $IndexerUrl/health" -ForegroundColor Yellow
}
