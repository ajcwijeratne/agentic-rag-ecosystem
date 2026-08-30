@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM  Command Centre update - 16 Aug 2026 (v2 - fixes an incomplete
REM  Governance/Memory nav merge, and logs everything to a file so
REM  we can see what happened even if this window closes)
REM  Run this on wijwork OR wijerco (double-click, or from an elevated
REM  PowerShell/cmd session). Safe to re-run. REPO auto-detects each
REM  machine's clone path below; pass a path as the first argument to
REM  override.
REM
REM  What it does:
REM   1. Copies the updated command_centre.html + sw.js into
REM      %REPO%\ui\  (Governance and Memory are now
REM      fully folded into Operating / Knowledge base - the first
REM      version of this file missed a second nav list, this one
REM      doesn't)
REM   2. Replaces docker-compose.yml with the updated version:
REM      Qdrant/Ollama healthcheck fix, plus n8n mounting the whole
REM      repo at /host-repo with fs/path access, for the webhook below
REM   3. Recreates qdrant, ollama, and n8n
REM   4. Imports the remote-deploy webhook workflow and credential
REM      into n8n, activates it, deletes the plaintext credential file
REM   5. Verifies all of it, end to end, including the webhook itself
REM
REM  Everything this window prints is also saved to deploy_log.txt
REM  next to this file. If anything looks wrong, send me that file.
REM ============================================================

REM  REPO auto-detects between wijwork's clone (C:\dev\agentic-rag) and
REM  wijerco's clone (C:\dev\agentic-rag-ecosystem) so this script runs
REM  unmodified on either machine. Pass a path as %1 to override.
if not "%~1"=="" (
    set REPO=%~1
) else if exist "C:\dev\agentic-rag-ecosystem\docker-compose.yml" (
    set REPO=C:\dev\agentic-rag-ecosystem
) else (
    set REPO=C:\dev\agentic-rag
)
set SRC=%~dp0
set LOG=%SRC%deploy_log.txt

call :main > "%LOG%" 2>&1
type "%LOG%"
echo.
echo Full log also saved to: %LOG%
echo.
pause
goto :eof

:main
echo ============================================
echo  Command Centre deploy - started %DATE% %TIME%
echo  Repo: %REPO%
echo ============================================

echo.
echo [1/6] Copying updated UI files into %REPO%\ui ...
copy /Y "%SRC%command_centre.html" "%REPO%\ui\command_centre.html"
if errorlevel 1 (
  echo   FAILED to copy command_centre.html. Check %REPO%\ui exists and is writable.
  goto :eof
)
copy /Y "%SRC%sw.js" "%REPO%\ui\sw.js"
if errorlevel 1 (
  echo   FAILED to copy sw.js.
  goto :eof
)
echo   OK - UI files copied.

echo.
echo [2/6] Replacing docker-compose.yml with the updated version ...
copy /Y "%SRC%docker-compose.yml" "%REPO%\docker-compose.yml"
if errorlevel 1 (
  echo   FAILED to copy docker-compose.yml.
  goto :eof
)
echo   OK.

echo.
echo [3/6] Copying the remote-deploy webhook workflow into n8n\workflows ...
if not exist "%REPO%\n8n\workflows" mkdir "%REPO%\n8n\workflows"
copy /Y "%SRC%remote_deploy.json" "%REPO%\n8n\workflows\remote_deploy.json"
copy /Y "%SRC%remote_deploy_credentials.json" "%REPO%\n8n\workflows\remote_deploy_credentials.json"
if errorlevel 1 (
  echo   FAILED to copy the n8n workflow/credential files.
  goto :eof
)
echo   OK.

echo.
echo [4/6] Recreating qdrant, ollama, n8n (this can take 20-30s) ...
pushd "%REPO%"
docker compose up -d qdrant ollama n8n
if errorlevel 1 (
  echo   FAILED - docker compose up returned an error. Is Docker Desktop running?
  popd
  goto :eof
)
popd
echo   OK. Waiting 20s for n8n to finish booting before talking to its CLI ...
timeout /t 20 /nobreak

echo.
echo [5/6] Importing the webhook credential and workflow, then activating ...
docker compose -f "%REPO%\docker-compose.yml" exec n8n n8n import:credentials --input=/home/node/.n8n/workflows/remote_deploy_credentials.json
if errorlevel 1 echo   WARNING: credential import returned a non-zero exit code, see output above.
docker compose -f "%REPO%\docker-compose.yml" exec n8n n8n import:workflow --input=/home/node/.n8n/workflows/remote_deploy.json
if errorlevel 1 echo   WARNING: workflow import returned a non-zero exit code, see output above.
docker compose -f "%REPO%\docker-compose.yml" exec n8n n8n publish:workflow --id=remote-deploy-file-write-v1
if errorlevel 1 echo   WARNING: publish returned a non-zero exit code, see output above.
docker compose -f "%REPO%\docker-compose.yml" restart n8n
echo   Waiting 15s for n8n to restart with the workflow active ...
timeout /t 15 /nobreak
del /F /Q "%REPO%\n8n\workflows\remote_deploy_credentials.json" >nul 2>&1
del /F /Q "%SRC%remote_deploy_credentials.json" >nul 2>&1
echo   Plaintext credential file deleted from both locations.

echo.
echo [6/6] Verifying ...
curl -s -o nul -w "  /health                    -> HTTP %%{http_code} (want 200)\n" http://localhost:8000/health
curl -s -o nul -w "  /kb/overview               -> HTTP %%{http_code} (want 200)\n" http://localhost:8000/kb/overview
curl -s -o nul -w "  /app/command_centre.html   -> HTTP %%{http_code} (want 200)\n" http://localhost:8000/app/command_centre.html
findstr /C:"cc-shell-v7" "%REPO%\ui\sw.js" >nul && echo   sw.js cache version         -> v7 confirmed on disk || echo   sw.js cache version         -> NOT v7, copy step may have failed
findstr /C:"id:'governance'" "%REPO%\ui\command_centre.html" >nul && echo   governance nav entry        -> STILL PRESENT, this is the bug, tell Claude || echo   governance nav entry        -> gone, correct
curl -s -o nul -w "  n8n deploy webhook          -> HTTP %%{http_code} (want 403, no secret sent - means it's active)\n" -X POST http://localhost:5678/webhook/deploy-file

echo.
echo ============================================
echo  Finished %DATE% %TIME%
echo  Now open http://localhost:8000/app/command_centre.html
echo  and hard-refresh (Ctrl+F5) once.
echo ============================================
goto :eof
