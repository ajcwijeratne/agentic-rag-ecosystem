@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM  wijerco voice setup - 27 Sep 2026
REM
REM  Makes Apex's voice work when the Command Centre is served by
REM  wijerco. Before this, wijerco's voice service ran with no
REM  speech recognition installed (no faster-whisper, no VOSK), so
REM  the mic could not be heard there, and only the browser voice
REM  could speak.
REM
REM   1. Snapshots installed packages (pip freeze) for rollback.
REM   2. Installs the CPU-only voice packages: faster-whisper, vosk,
REM      webrtcvad-wheels, kokoro-onnx. No torch.
REM   3. Runs deploy\wijerco-update.bat (a copy, so the pull cannot
REM      rewrite the script mid-run): pull, import check that rolls
REM      the pull back on failure, restart the core services.
REM   4. Fetches models: Whisper base (~145 MB), VOSK small (~40 MB),
REM      Kokoro fp32 (~340 MB, into %%LOCALAPPDATA%%\wijerco\kokoro).
REM   5. Checks the voice endpoints and times one spoken sentence.
REM   6. Sends this log back to wijwork.
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
echo  wijerco voice setup - started %DATE% %TIME%
echo  Repo: %REPO%
echo ============================================
set "PY=%REPO%\.venv\Scripts\python.exe"
if not exist "%PY%" ( echo   No venv at %PY%. Nothing changed. & goto :eof )
powershell -NoProfile -Command "$c=Get-CimInstance Win32_Processor; $m=Get-CimInstance Win32_OperatingSystem; '  CPU: {0} ({1} cores)' -f $c.Name,$c.NumberOfCores; '  RAM: {0:N1} GB total, {1:N1} GB free' -f ($m.TotalVisibleMemorySize/1MB),($m.FreePhysicalMemory/1MB)"

echo.
echo [1/6] Snapshot of installed packages ...
"%PY%" -m pip freeze > "%~dp0wijerco_pip_before_voice.txt"
echo   Saved to %~dp0wijerco_pip_before_voice.txt

echo.
echo [2/6] Installing voice packages ^(CPU only, no torch^) ...
"%PY%" -m pip install --disable-pip-version-check "faster-whisper>=1.0,<2" "vosk>=0.3.45" "webrtcvad-wheels>=2.0.14" "kokoro-onnx>=0.6.1,<0.7"
if errorlevel 1 echo   pip reported an error. Continuing: the update below import-checks before restarting anything.

echo.
echo [3/6] Pulling the new code and restarting services ...
copy /y "%REPO%\deploy\wijerco-update.bat" "%TEMP%\wijerco-update-run.bat" >nul
call "%TEMP%\wijerco-update-run.bat" "%REPO%" < nul
if not exist "%REPO%\media\tts.py" (
  echo   The pull did not land ^(see the update log above^). Stopping here.
  goto :eof
)

echo.
echo [4/6] Fetching models ...
pushd "%REPO%"
"%PY%" -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8'); print('  whisper base ready')"
"%PY%" -m media.vosk_engine --download
"%PY%" -m media.tts --fetch
popd

echo.
echo [5/6] Checking ...
echo   /voice/engines:
curl -s http://localhost:8000/voice/engines
echo.
echo   /voice/tts/status:
curl -s http://localhost:8000/voice/tts/status
echo.
echo   Kokoro timing on this machine ^(first run loads the model^):
pushd "%REPO%"
"%PY%" -m media.tts --say "Apex is online on wijerco. Hold the space bar to talk." --out "%TEMP%\apex_check.wav"
"%PY%" -m media.tts --say "Morning Aaron. Two approvals are waiting for you." --out "%TEMP%\apex_check2.wav"
popd
curl -s -o nul -w "  POST /voice/tts through the orchestrator -> HTTP %%{http_code} in %%{time_total}s\n" -H "Content-Type: application/json" -d "{\"text\":\"Morning Aaron. Two approvals are waiting.\"}" http://localhost:8000/voice/tts

echo.
echo [6/6] Next: on any tailnet device open
echo   https://wijerco.taila4c185.ts.net/app/command_centre.html
echo   hard refresh ^(Ctrl+Shift+R^), click once, then hold Space and talk.
echo.
echo ============================================
echo  Finished %DATE% %TIME%
echo ============================================
goto :eof
