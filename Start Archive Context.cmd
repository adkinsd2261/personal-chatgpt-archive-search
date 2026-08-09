@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\start_archive_context.ps1"
if errorlevel 1 (
  echo.
  echo Archive Context did not start. Leave this window open and send the error to Codex.
  pause
)
endlocal
