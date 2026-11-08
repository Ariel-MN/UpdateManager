# =============================================================================
#  Update Manager installer  --  DEDICATED TO: WINDOWS  (PowerShell)
#  Robust one-shot setup: finds a usable Python (prefers the 'py' launcher and
#  avoids the Microsoft Store stub), creates <app>\env, installs dependencies,
#  and verifies them.  Run:
#     powershell -ExecutionPolicy Bypass -File install.ps1
# =============================================================================
$ErrorActionPreference = 'Stop'
$AppDir = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $AppDir

function Fail($msg) { Write-Host "`nERROR: $msg" -ForegroundColor Red; exit 1 }

# --- find a usable Python 3 ---------------------------------------------------
$PyExe = $null; $PyArgs = @()
$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) {
    & py -3 -c "import sys" *> $null
    if ($LASTEXITCODE -eq 0) { $PyExe = 'py'; $PyArgs = @('-3') }
}
if (-not $PyExe) {
    $python = Get-Command python -ErrorAction SilentlyContinue
    # The Microsoft Store stub lives under WindowsApps and does not really run.
    if ($python -and ($python.Source -notlike '*WindowsApps*')) { $PyExe = 'python'; $PyArgs = @() }
}
if (-not $PyExe) {
    Fail "No usable Python 3 found. Install it from https://www.python.org/downloads/ and tick 'Add python.exe to PATH' (the Microsoft Store alias does not work)."
}
Write-Host "Using Python: $PyExe $($PyArgs -join ' ')"

# --- ensure venv + ensurepip --------------------------------------------------
& $PyExe @PyArgs -c "import venv, ensurepip" *> $null
if ($LASTEXITCODE -ne 0) { Fail "This Python lacks venv/ensurepip. Reinstall Python 3 from python.org." }

# --- (re)create the virtualenv inside UpdateManager\ -------------------------
if (Test-Path 'env') { Write-Host "Removing old env ..."; Remove-Item -Recurse -Force 'env' }
Write-Host "Creating virtualenv in $AppDir\env ..."
& $PyExe @PyArgs -m venv env
if ($LASTEXITCODE -ne 0) { Fail "Virtualenv creation failed." }

$EnvPy = Join-Path $AppDir 'env\Scripts\python.exe'
if (-not (Test-Path $EnvPy)) { Fail "venv python not found at $EnvPy." }

& $EnvPy -m pip install --upgrade pip wheel
if ($LASTEXITCODE -ne 0) { Fail "Upgrading pip/wheel failed." }

Write-Host "Installing dependencies ..."
& $EnvPy -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail "Installing dependencies failed (see the errors above)." }

# --- smoke test (tkinter ships with the python.org Windows build) -------------
Write-Host "Verifying installation ..."
& $EnvPy -c "import customtkinter, requests, PIL, tkinter; print('  imports OK  |  Pillow', PIL.__version__, ' requests', requests.__version__)"
if ($LASTEXITCODE -ne 0) { Fail "Dependencies are missing after install." }

Write-Host "`nDone. Launch with:  launchers\windows\UpdateManager.vbs  (silent, no console)" -ForegroundColor Green
Write-Host "   or:  launchers\windows\UpdateManager.bat"
