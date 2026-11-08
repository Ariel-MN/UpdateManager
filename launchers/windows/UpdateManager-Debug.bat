@echo off
REM ===========================================================================
REM   Update Manager DEBUG launcher  --  DEDICATED TO: WINDOWS
REM   Keeps the console open and shows all output/errors (uses python, not
REM   pythonw), then pauses so a traceback stays readable.
REM ===========================================================================
setlocal
set "APP_DIR=%~dp0..\.."
set "PY=python"
if exist "%APP_DIR%\env\Scripts\python.exe" (
  set "PY=%APP_DIR%\env\Scripts\python.exe"
) else if exist "%APP_DIR%\..\env\Scripts\python.exe" (
  set "PY=%APP_DIR%\..\env\Scripts\python.exe"
)
echo === Update Manager (debug) ===
"%PY%" --version
echo App: %APP_DIR%\updatemanager.py
echo -------------------------------------------
"%PY%" "%APP_DIR%\updatemanager.py"
echo -------------------------------------------
echo Exited with code %ERRORLEVEL%
pause
endlocal
