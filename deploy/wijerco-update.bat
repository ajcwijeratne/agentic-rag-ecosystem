@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM  wijerco update v3 - 4 Sep 2026
REM
REM  v3 changes, both learned from the live runs:
REM   - untracked files no longer block. wijerco has five stray
REM     files in the repo root left by an interrupted deploy;
REM     they cannot be overwritten by a fast-forward pull, and
REM     git refuses safely if an incoming file ever collides.
REM     Tracked modifications DO still stop it.
REM   - the pull is now reversible. wijerco is 36 commits behind
REM     and requirements.txt grew by 22 lines, so if the code no
REM     longer imports on this machine the script resets back to
REM     exactly where it started rather than leaving files on
REM     disk that a later restart would pick up broken.
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
echo  wijerco update v3 - started %DATE% %TIME%
echo  Repo: %REPO%
echo ============================================

echo.
echo [0/6] Locating git ...
set "GITEXE="
where git >nul 2>&1 && set "GITEXE=git"
if not defined GITEXE if exist "%ProgramFiles%\Git\cmd\git.exe" set "GITEXE=%ProgramFiles%\Git\cmd\git.exe"
if not defined GITEXE if exist "%LOCALAPPDATA%\Programs\Git\cmd\git.exe" set "GITEXE=%LOCALAPPDATA%\Programs\Git\cmd\git.exe"
if not defined GITEXE for /d %%d in ("%LOCALAPPDATA%\GitHubDesktop\app-*") do if exist "%%d\resources\app\git\cmd\git.exe" set "GITEXE=%%d\resources\app\git\cmd\git.exe"
if not defined GITEXE ( echo   NOT FOUND. Nothing changed. & goto :eof )
echo   Using: !GITEXE!

pushd "%REPO%"

echo.
echo [1/6] Current state ...
"!GITEXE!" rev-parse --abbrev-ref HEAD
for /f %%i in ('"!GITEXE!" rev-parse HEAD') do set PREV=%%i
echo   HEAD before: !PREV!
"!GITEXE!" log --oneline -1
"!GITEXE!" status --porcelain > "%TEMP%\wj_status.txt" 2>&1
if errorlevel 1 ( echo   STOPPING: git status failed. & type "%TEMP%\wj_status.txt" & popd & goto :eof )

findstr /v /b /c:"??" "%TEMP%\wj_status.txt" > "%TEMP%\wj_tracked.txt"
set TRACKED=0
for /f %%i in ('type "%TEMP%\wj_tracked.txt" ^| find /c /v ""') do set TRACKED=%%i
set UNTRACKED=0
for /f %%i in ('type "%TEMP%\wj_status.txt" ^| find /c /v ""') do set UNTRACKED=%%i
echo   Tracked modifications: !TRACKED!   (these would block)
echo   Untracked files:       !UNTRACKED! total lines incl. untracked (these do not)
if not "!TRACKED!"=="0" (
  echo   STOPPING: tracked files are modified. Commit or stash first.
  type "%TEMP%\wj_tracked.txt"
  popd
  goto :eof
)
echo   OK - no tracked modifications, safe to fast-forward.
if exist "%REPO%\remote_deploy_credentials.json" (
  echo.
  echo   ******************************************************
  echo   SECURITY: remote_deploy_credentials.json is sitting in
  echo   the repo root in plaintext, left by an interrupted
  echo   deploy. The runbook says to remove it by hand. Run:
  echo       del "%REPO%\remote_deploy_credentials.json"
  echo   Not deleting it automatically - your call.
  echo   ******************************************************
)

echo.
echo [2/6] Fetching, and what is about to land ...
"!GITEXE!" fetch origin
for /f %%i in ('"!GITEXE!" rev-list --count HEAD..origin/main') do set BEHIND=%%i
echo   !BEHIND! commit^(s^) to pull.
"!GITEXE!" --no-pager log --oneline HEAD..origin/main

echo.
echo [3/6] Pulling ...
"!GITEXE!" pull --ff-only origin main
if errorlevel 1 ( echo   FAILED - not a fast-forward. Nothing changed. & popd & goto :eof )
"!GITEXE!" log --oneline -1
echo   OK.

echo.
echo [4/6] Import check ^(this decides whether we keep the pull^) ...
.venv\Scripts\python.exe -c "import orchestrator.main; print('   orchestrator imports OK')"
if errorlevel 1 (
  echo.
  echo   IMPORT FAILED. Rolling back to !PREV! so this machine is
  echo   left exactly as it was, rather than holding code that a
  echo   later restart would run broken.
  "!GITEXE!" reset --hard !PREV!
  "!GITEXE!" log --oneline -1
  echo.
  echo   Likely a missing dependency. To try again:
  echo       .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo   then re-run this script. Services were NOT restarted.
  popd
  goto :eof
)
.venv\Scripts\python.exe -c "from memory.memory_agent import extract_and_record_evidence, CANDIDATE_KIND; from memory.consolidation import verify_candidates; print('   memory verification gate present')"

echo.
echo [5/6] Restarting the seven core services ^(daemon/channels untouched^) ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%REPO%\scripts\start_all.ps1"
echo   Waiting 30s ...
timeout /t 30 /nobreak >nul

echo.
echo [6/6] Verifying ...
curl -s -o nul -w "  /health                  -> HTTP %%{http_code} (want 200)\n" http://localhost:8000/health
curl -s -o nul -w "  /wijerco/roster          -> HTTP %%{http_code} (want 200)\n" http://localhost:8000/wijerco/roster
curl -s -o nul -w "  /app/command_centre.html -> HTTP %%{http_code} (want 200)\n" http://localhost:8000/app/command_centre.html

echo.
echo ============================================
echo  Finished %DATE% %TIME%
echo ============================================
popd
goto :eof