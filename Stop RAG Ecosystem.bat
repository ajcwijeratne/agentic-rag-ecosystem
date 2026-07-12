@echo off
REM ===========================================================================
REM  Double-click this to gracefully stop the Agentic RAG Ecosystem.
REM  Stops the Python services and brings the Docker stack down (data kept).
REM ===========================================================================
title Agentic RAG Ecosystem - Shutdown
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\shutdown.ps1"
