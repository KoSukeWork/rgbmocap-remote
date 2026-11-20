@echo off
setlocal DisableDelayedExpansion
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONNOUSERSITE=1"
cd /d "%~dp0"
set "STOREDIR=%~dp0data"
if defined RGBMOCAP_REMOTE_STATE set "STOREDIR=%RGBMOCAP_REMOTE_STATE%"
if "%~1"=="" (
  echo Usage: Create-User.cmd artist
  exit /b 2
)
"%~dp0python\python.exe" -B -X utf8 -m service --data "%STOREDIR%" create-user "%~1"
exit /b %ERRORLEVEL%
