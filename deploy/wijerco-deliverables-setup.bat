@echo off
REM One-time setup on wijerco for Deliverables. See wijerco-deliverables-setup.ps1.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0wijerco-deliverables-setup.ps1"
echo.
pause
