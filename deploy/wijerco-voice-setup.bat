@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM  wijerco voice setup v2 - 27 Sep 2026
REM
REM  Makes Apex's voice work when the Command Centre is served by
REM  wijerco. Safe to re-run: every step skips what is done.
REM
REM   1. Snapshots installed packages once (pip freeze), for rollback.
REM   2. Installs the CPU-only voice packages: faster-whisper, vosk,
REM      webrtcvad-wheels, kokoro-onnx. No torch.
REM   3. Runs wijerco-update.bat (the repo copy, else one beside this
REM      script; from a temp copy so the pull cannot rewrite it
REM      mid-run): pull, import check that rolls the pull back on
REM      failure, restart the core services.
REM   4. Checks the native libraries load (media.tts --doctor). v1
REM      found onnxruntime and ctranslate2 installed but unloadable:
REM      the Microsoft Visual C++ runtime was missing. If anything
REM      fails, installs that runtime (Windows asks: click Yes),
REM      re-checks, and restarts the services.
REM   5. Fetches models: Whisper base, VOSK small, Kokoro fp32.
REM   6. Checks: engines, Kokoro timing, speech in -> text out.
REM   7. Sends this log back to wijwork.
REM
REM  Undo the packages:
REM   .venv\Scripts\python.exe -m pip uninstall -y faster-whisper vosk webrtcvad-wheels kokoro-onnx
REM ============================================================

if not "%~1"=="" (
    set "REPO=%~1"
) else if exist "C:\dev\agentic-rag-ecosystem\docker-compose.yml" (
    set "REPO=C:\dev\agentic-rag-ecosystem"
) else (
    set "REPO=C:\dev\agentic-rag"
)
set "LOG=%~dp0wijerco_voice_setup_log.txt"

echo Working. This takes a few minutes. If Windows asks for permission
echo to install the Microsoft Visual C++ runtime, click Yes.
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
echo  wijerco voice setup v2 - started %DATE% %TIME%
echo  Repo: %REPO%
echo ============================================
set "PY=%REPO%\.venv\Scripts\python.exe"
if not exist "%PY%" ( echo   No venv at %PY%. Nothing changed. & goto :eof )
powershell -NoProfile -Command "$c=Get-CimInstance Win32_Processor; $m=Get-CimInstance Win32_OperatingSystem; '  CPU: {0} ({1} cores)' -f $c.Name,$c.NumberOfCores; '  RAM: {0:N1} GB total, {1:N1} GB free' -f ($m.TotalVisibleMemorySize/1MB),($m.FreePhysicalMemory/1MB)"

echo.
echo [1/7] Snapshot of installed packages ...
if exist "%~dp0wijerco_pip_before_voice.txt" (
  echo   Kept the first snapshot: %~dp0wijerco_pip_before_voice.txt
) else (
  "%PY%" -m pip freeze > "%~dp0wijerco_pip_before_voice.txt"
  echo   Saved to %~dp0wijerco_pip_before_voice.txt
)

echo.
echo [2/7] Voice packages ^(CPU only, no torch^) ...
"%PY%" -m pip install --disable-pip-version-check -q "faster-whisper>=1.0,<2" "vosk>=0.3.45" "webrtcvad-wheels>=2.0.14" "kokoro-onnx>=0.6.1,<0.7"
if errorlevel 1 echo   pip reported an error. Continuing: the update below import-checks before restarting anything.

echo.
echo [3/7] Pulling the new code and restarting services ...
REM The repo copy first: a stale wijerco-update.bat from an older Taildrop
REM can sit in Downloads (v2 run 27 Sep picked one up that had no git lookup).
set "UPD=%REPO%\deploy\wijerco-update.bat"
if not exist "!UPD!" set "UPD=%~dp0wijerco-update.bat"
if not exist "!UPD!" ( echo   wijerco-update.bat not found next to this script or in the repo. Stopping. & goto :eof )
echo   Using !UPD!
copy /y "!UPD!" "%TEMP%\wijerco-update-run.bat" >nul
call "%TEMP%\wijerco-update-run.bat" "%REPO%" < nul
if not exist "%REPO%\media\tts.py" (
  echo   The pull did not land ^(see the update log above^). Stopping here.
  goto :eof
)
echo   Waiting 30s for the services to come up ...
powershell -NoProfile -Command "Start-Sleep 30"

echo.
echo [4/7] Can the voice libraries load? ...
pushd "%REPO%"
"%PY%" -m media.tts --doctor
if errorlevel 1 (
  echo.
  echo   Some native libraries will not load. Installing the Microsoft
  echo   Visual C++ runtime ^(x64^). Windows asks for permission: click Yes.
  curl -sSL -o "%TEMP%\vc_redist.x64.exe" https://aka.ms/vs/17/release/vc_redist.x64.exe
  "%TEMP%\vc_redist.x64.exe" /install /passive /norestart
  echo   Installer exit code !errorlevel! ^(0 installed, 1638 already current, 3010 installed and wants a restart^)
  echo   Re-checking ...
  "%PY%" -m media.tts --doctor
  echo   Restarting services so they load the libraries ...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%REPO%\scripts\start_all.ps1"
  powershell -NoProfile -Command "Start-Sleep 30"
)
popd

echo.
echo [5/7] Models ...
pushd "%REPO%"
"%PY%" -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8'); print('  whisper base ready')"
"%PY%" -m media.vosk_engine --download
"%PY%" -m media.tts --fetch
popd

echo.
echo [6/7] Checking ...
echo   /voice/engines:
curl -s http://localhost:8000/voice/engines
echo.
echo   /voice/tts/status:
curl -s http://localhost:8000/voice/tts/status
echo.
echo   Kokoro timing on this machine ^(the first run loads the model^):
pushd "%REPO%"
"%PY%" -m media.tts --say "Apex is online on wijerco. Hold the space bar to talk." --out "%TEMP%\apex_check.wav"
"%PY%" -m media.tts --say "Morning Aaron. Two approvals are waiting for you." --out "%TEMP%\apex_check2.wav"
popd
curl -s -o nul -w "  POST /voice/tts through the orchestrator -> HTTP %%{http_code} in %%{time_total}s\n" -H "Content-Type: application/json" -d "{\"text\":\"Morning Aaron. Two approvals are waiting.\"}" http://localhost:8000/voice/tts
echo   Speech in, text out ^(Kokoro's own sentence back through Whisper^):
if exist "%TEMP%\apex_check.wav" (
  curl -s -w "\n  HTTP %%{http_code} in %%{time_total}s\n" -F "file=@%TEMP%\apex_check.wav" -F "engine=whisper" http://localhost:8000/voice/transcribe
) else (
  echo   Skipped: Kokoro did not produce a test file.
)
echo.
echo   pip check ^(dependency conflicts, for the record^):
"%PY%" -m pip check

echo.
echo [7/7] Next: on any tailnet device open
echo   https://wijerco.taila4c185.ts.net/app/command_centre.html
echo   hard refresh ^(Ctrl+Shift+R^), click once, then hold Space and talk.
echo.
echo ============================================
echo  Finished %DATE% %TIME%
echo ============================================
goto :eof
