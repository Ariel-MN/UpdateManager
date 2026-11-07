@echo off
REM ===========================================================================
REM   Update Manager launcher  --  DEDICATED TO: WINDOWS  (Command Prompt)
REM   Runs the app with pythonw (no console window). Uses the shared
REM   virtualenv at <Crypto>\env if present, otherwise the system Python.
REM ===========================================================================
setlocal
set "APP_DIR=%~dp0..\.."
if exist "%APP_DIR%\..\env\Scripts\activate.bat" call "%APP_DIR%\..\env\Scripts\activate.bat"
start "" pythonw "%APP_DIR%\updatemanager.py"
endlocal
