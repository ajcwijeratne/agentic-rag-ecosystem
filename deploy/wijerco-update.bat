@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM  wijerco update - 4 Sep 2026
REM  Pulls main and restarts the core services. Safe to re-run.
REM
REM  Refuses to do anything risky:
REM    - stops if the working tree is dirty (never overwrites your changes)
REM    - pulls fast-forward only
REM    - import-checks the orchestrator BEFORE restarting anything
REM    - restarts only the seven core services, not the daemon or channels
REM
REM  The log is sent back to wijwork automatically at the end.
REM ============================================================

if not "%~1"=="" (
    set REPO=%~1
) else if exist "C:\dev\agentic-rag-ecosystem\docker-compose.yml" (
    set REPO=C:\dev\agentic-rag-ecosystem
) else (
    set REPO=C:\dev\agentic-rag
)
set LOG=%~dp0wijerco_update_log.txt

call :main > "%LOG%" 2>&1
type "%LOG%"
echo.
echo Sending this log back to wijwork ...
"C:\Program Files\Tailscale\tailscale.exe" file cp "%LOG%" wijwork:
echo.
echo Done. Log also saved to: %LOG%
echo.
pause
goto :eof

:main
echo ============================================
echo  wijerco update - started %DATE% %TIME%
echo  Repo: %REPO%
echo ============================================
pushd "%REPO%"

echo.
echo [1/5] Current state ...
git rev-parse --abbrev-ref HEAD
git log --oneline -1
echo   Working tree:
git status --short
for /f %%i in ('git status --porcelain ^| find /c /v ""') do set DIRTY=%%i
if not "!DIRTY!"=="0" (
  echo.
  echo   STOPPING: working tree has !DIRTY! uncommitted change^(s^).
  echo   Pulling could overwrite them. Commit or stash here first.
  popd
  goto :eof
)
echo   OK - tree is clean.

echo.
echo [2/5] Fetching, and what is about to land ...
git fetch origin
git --no-pager log --oneline HEAD..origin/main
for /f %%i in ('git rev-list --count HEAD..origin/main') do set BEHIND=%%i
echo   !BEHIND! commit^(s^) to pull.

echo.
echo [3/5] Pulling ...
git pull --ff-only origin main
if errorlevel 1 (
  echo   FAILED - pull did not fast-forward. Resolve here by hand.
  popd
  goto :eof
)
git log --oneline -1
echo   OK.

echo.
echo [4/5] Import check BEFORE restarting anything ...
.venv\Scripts\python.exe -c "import orchestrator.main; print('   orchestrator imports OK')"
if errorlevel 1 (
  echo.
  echo   FAILED - orchestrator does not import on this machine.
  echo   Nothing was restarted; the running system is untouched.
  popd
  goto :eof
)
.venv\Scripts\python.exe -c "from memory.memory_agent import extract_and_record_evidence, CANDIDATE_KIND; from memory.consolidation import verify_candidates; print('   memory verification gate present')"
if errorlevel 1 echo   WARNING: memory gate did not import, see error above.

echo.
echo [5/5] Restarting the seven core services ^(daemon and channels untouched^) ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%REPO%\scripts\start_all.ps1"
echo   Waiting 30s for the orchestrator to bind ...
timeout /t 30 /nobreak >nul

echo.
echo Verifying ...
curl -s -o nul -w "  /health                  -> HTTP %%{http_code} (want 200)\n" http://localhost:8000/health
curl -s -o nul -w "  /wijerco/roster          -> HTTP %%{http_code} (want 200)\n" http://localhost:8000/wijerco/roster
curl -s -o nul -w "  /app/command_centre.html -> HTTP %%{http_code} (want 200)\n" http://localhost:8000/app/command_centre.html

echo.
echo ============================================
echo  Finished %DATE% %TIME%
echo  Open the Command Centre and hard-refresh once (Ctrl+F5).
echo ============================================
popd
goto :eof