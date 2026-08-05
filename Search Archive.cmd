@echo off
setlocal
"%~dp0.venv\Scripts\python.exe" "%~dp0tools\search_archive.py" %*
exit /b %ERRORLEVEL%

