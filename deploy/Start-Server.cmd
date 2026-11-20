@echo off
setlocal DisableDelayedExpansion
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONNOUSERSITE=1"
cd /d "%~dp0"
set "STOREDIR=%~dp0data"
if defined RGBMOCAP_REMOTE_STATE set "STOREDIR=%RGBMOCAP_REMOTE_STATE%"
set "PATH=%~dp0git\cmd;%PATH%"
"%~dp0python\python.exe" -I -B -X utf8 "%~dp0check_server.py" --quick
if errorlevel 1 exit /b 1
"%~dp0python\python.exe" -B -X utf8 -m service --data "%STOREDIR%" serve --upstream "%~dp0engine" %*
exit /b %ERRORLEVEL%
