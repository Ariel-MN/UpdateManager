#!/usr/bin/env bash
# =============================================================================
#  Update Manager installer  --  DEDICATED TO: macOS
#  Robust one-shot setup: checks Python, ensures venv + Tk (auto via Homebrew
#  when possible), creates <app>/env, installs dependencies, and verifies them.
#  First time: make it executable, then double-click:  chmod +x install.command
# =============================================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"   # UpdateManager/
cd "$APP_DIR"

# A .zip download or a copy from Windows can drop the executable bit. Restore it for the
# installer and the launchers, so running via `bash install.command` is always enough.
chmod +x install/macos/install.command launchers/macos/*.command 2>/dev/null || true

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die()  { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# --- Python present? ----------------------------------------------------------
command -v python3 >/dev/null 2>&1 || die "python3 not found. Install Python 3 (brew install python, or https://www.python.org/downloads/) and re-run."
python3 -c 'import venv, ensurepip' >/dev/null 2>&1 || die "This python3 lacks venv/ensurepip. Reinstall Python 3 from python.org or Homebrew."
say "Using python3 $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])') ($(command -v python3))"

# --- ensure Tk (the GUI needs tkinter); auto-resolve via Homebrew if possible -
if ! python3 -c 'import tkinter' >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    say "Installing Tk (python-tk) via Homebrew ..."
    brew install python-tk || true
  fi
  python3 -c 'import tkinter' >/dev/null 2>&1 \
    || die "tkinter is not available for this python3. Install a Tk-enabled Python (brew: 'brew install python python-tk', or the python.org installer) and re-run."
fi

# --- (re)create the virtualenv inside UpdateManager/ -------------------------
say "Creating virtualenv in $APP_DIR/env ..."
rm -rf env
python3 -m venv env
# shellcheck disable=SC1091
source env/bin/activate
python -m pip install --upgrade pip wheel

say "Installing dependencies ..."
pip install -r requirements.txt

# --- smoke test ---------------------------------------------------------------
say "Verifying installation ..."
python - <<'PY' || die "Dependencies are missing after install (see the errors above)."
import customtkinter, requests, PIL, tkinter  # noqa: F401
print("  imports OK  |  Pillow", PIL.__version__, " requests", requests.__version__)
PY

say "Done. Launch with:  ./launchers/macos/UpdateManager.command   (or: python updatemanager.py)"
