@echo off
REM Double-click launcher for the coding agent.
REM Opens a console window, starts the server (browser auto-opens), and stays
REM open so you can read any errors. Press Ctrl+C in this window to stop it.

cd /d "%~dp0"
title Coding Agent

REM Prefer the known Python install; fall back to whatever's on PATH.
set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=python"

REM Make sure we use the Claude subscription, not a billed API key.
set "ANTHROPIC_API_KEY="

echo Starting coding agent...  (browser opens automatically; Ctrl+C here to stop)
echo.
"%PY%" app.py %*

echo.
echo === Server stopped. Press any key to close this window. ===
pause >nul
