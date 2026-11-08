#!/usr/bin/env bash
# =============================================================================
#  Update Manager uninstaller  --  DEDICATED TO: macOS
#  Removes the virtualenv (env/) and caches created by the installer. Keeps your
#  source files, config.ini and secrets.ini.
#  Run:   bash install/macos/uninstall.command
# =============================================================================
set -u
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"   # UpdateManager/
cd "$APP_DIR"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "Removing virtualenv and caches ..."
rm -rf env img/.cache
find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

say "Done."
echo "  Kept: your source files, config.ini and secrets.ini."
echo "  To remove everything, delete this folder:  $APP_DIR"
