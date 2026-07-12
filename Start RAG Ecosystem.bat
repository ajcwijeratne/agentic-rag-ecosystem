@echo off
REM ===========================================================================
REM  Double-click this to boot the whole Agentic RAG Ecosystem.
REM  It runs scripts\launch.ps1 with the execution policy bypassed for this
REM  process only (no permanent system change).
REM ===========================================================================
title Agentic RAG Ecosystem - Launcher
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launch.ps1"
if errorlevel 1 (
    echo.
    echo Launcher reported an error. See the messages above.
    pause
)
