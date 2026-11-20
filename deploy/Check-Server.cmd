@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
"%~dp0python\python.exe" -I -B -X utf8 "%~dp0check_server.py"
exit /b %ERRORLEVEL%
