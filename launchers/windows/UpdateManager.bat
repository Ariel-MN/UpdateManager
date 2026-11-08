@echo off
REM ===========================================================================
REM   Update Manager launcher  --  DEDICATED TO: WINDOWS  (Command Prompt)
REM   Runs the app with pythonw (no console window). Uses a virtualenv if
REM   present: repo-local env\ first, then a shared ..\env; else system Python.
REM ===========================================================================
setlocal
set "APP_DIR=%~dp0..\.."
set "PYW=pythonw"
if exist "%APP_DIR%\env\Scripts\pythonw.exe" (
  set "PYW=%APP_DIR%\env\Scripts\pythonw.exe"
) else if exist "%APP_DIR%\..\env\Scripts\pythonw.exe" (
  set "PYW=%APP_DIR%\..\env\Scripts\pythonw.exe"
)
start "" "%PYW%" "%APP_DIR%\updatemanager.py"
endlocal
