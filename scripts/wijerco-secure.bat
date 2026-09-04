@echo off
REM Run on wijerco. Double-click. See scripts\wijerco-secure.ps1 for what it does.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0wijerco-secure.ps1"
echo.
pause
