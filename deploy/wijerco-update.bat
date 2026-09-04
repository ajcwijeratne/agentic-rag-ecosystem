@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM  wijerco update v2 - 4 Sep 2026
REM  v1 failed: git is not on PATH on wijerco (the repo was cloned
REM  with GitHub Desktop, which bundles its own git). v1 also
REM  misread that failure as "working tree is dirty", because the
REM  line count it used was counting git's error output. Both fixed.
REM
REM  Still refuses to do anything risky: stops on a dirty tree,
REM  pulls fast-forward only, import-checks BEFORE restarting, and
REM  leaves the daemon and channels alone.
REM ============================================================

if not "%~1"=="" (
    set "REPO=%~1"
) else if exist "C:\dev\agentic-rag-ecosystem\docker-compose.yml" (
    set "REPO=C:\dev\agentic-rag-ecosystem"
) else (
    set "REPO=C:\dev\agentic-rag"
)
set "LOG=%~dp0wijerco_update_log.txt"

call :main > "%LOG%" 2>&1
type "%LOG%"
echo.
echo Sending this log back to wijwork ...
"C:\Program Files\Tailscale\tailscale.exe" file cp "%LOG%" wijwork:
echo.
pause
goto :eof

:main
echo ============================================
echo  wijerco update v2 - started %DATE% %TIME%
echo  Repo: %REPO%
echo ============================================

echo.
echo [0/5] Locating git ...
set "GITEXE="
where git >nul 2>&1 && set "GITEXE=git"
if not defined GITEXE if exist "%ProgramFiles%\Git\cmd\git.exe" set "GITEXE=%ProgramFiles%\Git\cmd\git.exe"
if not defined GITEXE if exist "%ProgramFiles(x86)%\Git\cmd\git.exe" set "GITEXE=%ProgramFiles(x86)%\Git\cmd\git.exe"
if not defined GITEXE if exist "%LOCALAPPDATA%\Programs\Git\cmd\git.exe" set "GITEXE=%LOCALAPPDATA%\Programs\Git\cmd\git.exe"
if not defined GITEXE for /d %%d in ("%LOCALAPPDATA%\GitHubDesktop\app-*") do if exist "%%d\resources\app\git\cmd\git.exe" set "GITEXE=%%d\resources\app\git\cmd\git.exe"
if not defined GITEXE (
  echo   NOT FOUND. Looked on PATH, in Program Files, and inside GitHub Desktop.
  echo   Nothing was changed. Send this log back and we will find it.
  dir /b "%LOCALAPPDATA%\GitHubDesktop" 2>nul
  goto :eof
)
echo   Using: !GITEXE!

pushd "%REPO%"

echo.
echo [1/5] Current state ...
"!GITEXE!" rev-parse --abbrev-ref HEAD
"!GITEXE!" log --oneline -1
"!GITEXE!" status --porcelain > "%TEMP%\wj_status.txt" 2>&1
if errorlevel 1 (
  echo   STOPPING: git status failed. Output:
  type "%TEMP%\wj_status.txt"
  popd
  goto :eof
)
set DIRTY=0
for /f %%i in ('type "%TEMP%\wj_status.txt" ^| find /c /v ""') do set DIRTY=%%i
echo   Uncommitted changes: !DIRTY!
if not "!DIRTY!"=="0" (
  echo   Working tree contents:
  type "%TEMP%\wj_status.txt"
  echo.
  echo   STOPPING: commit or stash on wijerco first, then re-run.
  popd
  goto :eof
)
echo   OK - tree is clean.

echo.
echo [2/5] Fetching, and what is about to land ...
"!GITEXE!" fetch origin
"!GITEXE!" --no-pager log --oneline HEAD..origin/main
for /f %%i in ('"!GITEXE!" rev-list --count HEAD..origin/main') do set BEHIND=%%i
echo   !BEHIND! commit^(s^) to pull.

echo.
echo [3/5] Pulling ...
"!GITEXE!" pull --ff-only origin main
if errorlevel 1 (
  echo   FAILED - pull did not fast-forward. Resolve here by hand.
  popd
  goto :eof
)
"!GITEXE!" log --oneline -1
echo   OK.

echo.
echo [4/5] Import check BEFORE restarting anything ...
.venv\Scripts\python.exe -c "import orchestrator.main; print('   orchestrator imports OK')"
if errorlevel 1 (
  echo   FAILED - orchestrator does not import here. Nothing restarted.
  popd
  goto :eof
)
.venv\Scripts\python.exe -c "from memory.memory_agent import extract_and_record_evidence, CANDIDATE_KIND; from memory.consolidation import verify_candidates; print('   memory verification gate present')"
if errorlevel 1 echo   WARNING: memory gate did not import, see above.

echo.
echo [5/5] Restarting the seven core services ^(daemon/channels untouched^) ...
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
echo ============================================
popd
goto :eof