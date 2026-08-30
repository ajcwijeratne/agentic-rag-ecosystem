# =============================================================================
# Weekly automated live eval: golden + recall (Stage 1 item 15).
#
# Runs just the two live-marked retrieval-quality suites - tests/eval/
# test_rag_golden.py and tests/eval/test_retrieval_recall.py - not the full
# `-m live` set (that also includes tests/perf/test_cost_latency.py, a
# separate cost/latency regression concern with its own baseline file).
# The point of running this weekly rather than only "at the next debugging
# session" is that a retrieval regression should surface within days.
#
# Every run appends one line to logs/live_eval.jsonl (timestamp, pass/fail
# counts, exit code, elapsed time, and the tail of pytest's own output) so
# there's a record to look back over, and so a later monthly review (see
# orchestrator/review.py) or a human can see the trend rather than only the
# latest result. Needs Qdrant + Ollama up (same live-service requirement as
# any other `-m live` test) - if they're down, pytest reports the tests
# skipped, which this script records as skipped, not as a failure.
#
# Meant to run weekly via Task Scheduler - see scripts/register_scheduled_tasks.ps1.
# Safe to run by hand: .\scripts\weekly_live_eval.ps1
# =============================================================================

$ProjectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $ProjectRoot

$Py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Host "[fatal] venv interpreter not found at $Py" -ForegroundColor Red
    exit 1
}

New-Item -ItemType Directory -Force -Path "logs" | Out-Null
$Log = "logs\live_eval.jsonl"

Write-Host "=== Weekly live eval (golden + recall) starting ==="
$startTime = Get-Date

$output = & $Py -m pytest -m live -v "tests/eval/test_rag_golden.py" "tests/eval/test_retrieval_recall.py" 2>&1
$exitCode = $LASTEXITCODE
$elapsed = (Get-Date) - $startTime

$outputText = ($output | Out-String)
$summaryLine = ($output | Select-String -Pattern '^=+ .* =+$' | Select-Object -Last 1).ToString().Trim()

# Pull counts out of pytest's own summary line rather than re-deriving them,
# so this always agrees with what pytest itself reported.
$passed  = 0; $failed = 0; $skipped = 0; $errors = 0
if ($summaryLine -match '(\d+) passed')  { $passed  = [int]$Matches[1] }
if ($summaryLine -match '(\d+) failed')  { $failed  = [int]$Matches[1] }
if ($summaryLine -match '(\d+) skipped') { $skipped = [int]$Matches[1] }
if ($summaryLine -match '(\d+) error')   { $errors  = [int]$Matches[1] }

# "Passed" here means the suite actually ran and nothing failed - a fully
# skipped run (services down) is not a pass, it's a non-result, and should
# not be read as "regressions surface within days" when nothing was checked.
$ranSomething = ($passed + $failed) -gt 0
$ok = $ranSomething -and ($failed -eq 0) -and ($errors -eq 0) -and ($exitCode -eq 0)

$tail = ($outputText -split "`n" | Select-Object -Last 40) -join "`n"

$logEntry = [pscustomobject]@{
    ts          = (Get-Date).ToString("o")
    elapsed_sec = [math]::Round($elapsed.TotalSeconds, 1)
    exit_code   = $exitCode
    passed      = $passed
    failed      = $failed
    skipped     = $skipped
    errors      = $errors
    ok          = $ok
    summary     = $summaryLine
    output_tail = $tail
}
Add-Content -Path $Log -Value ($logEntry | ConvertTo-Json -Depth 4 -Compress)

Write-Host ""
Write-Host $summaryLine
if ($ok) {
    Write-Host "LIVE EVAL PASSED ($([math]::Round($elapsed.TotalSeconds,1))s)" -ForegroundColor Green
} elseif (-not $ranSomething) {
    Write-Host "LIVE EVAL DID NOT RUN - services likely down (all tests skipped)" -ForegroundColor Yellow
} else {
    Write-Host "LIVE EVAL FAILED - see $Log and the output above" -ForegroundColor Red
}

exit $exitCode
