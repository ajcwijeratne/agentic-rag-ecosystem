# Trigger vault re-index
param([string]$VaultPath = $env:OBSIDIAN_VAULT_PATH)
$IndexerUrl = if ($env:INDEXER_URL) { $env:INDEXER_URL } else { "http://localhost:8005" }

Write-Host "[index] Triggering vault index: $VaultPath" -ForegroundColor Cyan
$body = @{ vault_path = $VaultPath } | ConvertTo-Json
try {
    $resp = Invoke-RestMethod "$IndexerUrl/index" -Method Post -Body $body -ContentType "application/json"
    $resp | ConvertTo-Json -Depth 3
} catch {
    Write-Host "[error] $($_.Exception.Message)" -ForegroundColor Red
}
