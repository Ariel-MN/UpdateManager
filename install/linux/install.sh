#!/usr/bin/env bash
# =============================================================================
#  Update Manager installer  --  DEDICATED TO: LINUX / Raspberry Pi
#  Robust setup: checks Python, ensures venv + Tk, creates <app>/env, installs
#  dependencies (with a system-Pillow fallback), and verifies it all imports.
#  Run once:   chmod +x install.sh && ./install.sh
# =============================================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"   # UpdateManager/
cd "$APP_DIR"

# A .zip download or a copy from Windows can drop the executable bit. Restore it for the
# installer itself and the launchers, so running via `bash install.sh` is always enough.
chmod +x install/linux/install.sh launchers/linux/*.sh 2>/dev/null || true

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die()  { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# --- Python present? ----------------------------------------------------------
command -v python3 >/dev/null 2>&1 || die "python3 not found. Install it first (e.g. sudo apt install python3)."
PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
say "Using python3 $PYVER ($(command -v python3))"

# --- apt detection + sudo helper ---------------------------------------------
HAVE_APT=0; command -v apt-get >/dev/null 2>&1 && HAVE_APT=1
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
apt_install() {  # apt_install <pkg>...
  if [ "$HAVE_APT" -ne 1 ]; then
    die "Need system packages ($*) but 'apt-get' was not found. Install them with your package manager and re-run."
  fi
  $SUDO apt-get update -y
  $SUDO apt-get install -y "$@"
}

# --- ensure venv + ensurepip --------------------------------------------------
if ! python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then
  say "Installing python3-venv (needed to create a virtualenv) ..."
  apt_install python3-venv
fi

# --- ensure Tk (the GUI needs tkinter) ---------------------------------------
if ! python3 -c 'import tkinter' >/dev/null 2>&1; then
  say "Installing python3-tk (GUI toolkit) ..."
  apt_install python3-tk
fi

# --- (re)create the virtualenv inside UpdateManager/ -------------------------
say "Creating virtualenv in $APP_DIR/env ..."
rm -rf env
python3 -m venv env
# shellcheck disable=SC1091
source env/bin/activate
python -m pip install --upgrade pip wheel

# --- install dependencies, with a system-Pillow fallback ---------------------
say "Installing dependencies ..."
if ! pip install -r requirements.txt; then
  warn "pip failed (usually Pillow building from source with no wheel for this Python/arch)."
  say "Falling back to the prebuilt Pillow from the system (apt) ..."
  apt_install python3-pil python3-pil.imagetk python3-tk
  deactivate 2>/dev/null || true
  rm -rf env
  python3 -m venv env --system-site-packages
  # shellcheck disable=SC1091
  source env/bin/activate
  python -m pip install --upgrade pip wheel
  pip install customtkinter==5.2.2 CTkMessagebox==2.7 requests==2.32.3
fi

# --- smoke test: everything the app needs must import ------------------------
say "Verifying installation ..."
python - <<'PY' || die "Dependencies are missing after install (see the errors above)."
import customtkinter, requests, PIL, tkinter  # noqa: F401
print("  imports OK  |  Pillow", PIL.__version__, " requests", requests.__version__)
PY

say "Done. Launch with:  ./launchers/linux/updatemanager.sh   (or: python updatemanager.py)"
echo "     Menu entry:   edit launchers/linux/UpdateManager.desktop and copy it to ~/.local/share/applications"
