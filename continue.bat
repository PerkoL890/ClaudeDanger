@echo off
REM Double-click to CONTINUE your most recent session for the remembered
REM project (full prior context). The first message may be slower and cost
REM more, because the whole prior conversation is reloaded as context.
REM Press Ctrl+C in this window to stop.

cd /d "%~dp0"
title Coding Agent (continue last session)

set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=python"
set "ANTHROPIC_API_KEY="

echo Continuing your most recent session...  (Ctrl+C here to stop)
echo.
"%PY%" app.py --resume-last %*

echo.
echo === Server stopped. Press any key to close this window. ===
pause >nul
