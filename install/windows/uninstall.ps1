# =============================================================================
#  Update Manager uninstaller  --  DEDICATED TO: WINDOWS  (PowerShell)
#  Removes the virtualenv (env\), caches and the Desktop shortcut created by the
#  installer / CreateShortcut.vbs. Keeps your source files, config.ini and secrets.ini.
#  Run:   powershell -ExecutionPolicy Bypass -File install\windows\uninstall.ps1
# =============================================================================
$ErrorActionPreference = 'Stop'
$AppDir = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $AppDir

Write-Host "Removing virtualenv and caches ..."
foreach ($p in @('env', 'img\.cache')) { if (Test-Path $p) { Remove-Item -Recurse -Force $p } }
Get-ChildItem -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Update Manager.lnk'
if (Test-Path $lnk) { Remove-Item -Force $lnk; Write-Host "Removed Desktop shortcut." }

Write-Host ""
Write-Host "Done. Kept: your source files, config.ini and secrets.ini."
Write-Host "To remove everything, delete this folder: $AppDir"
