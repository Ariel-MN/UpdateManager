#!/usr/bin/env bash
# =============================================================================
#  Update Manager uninstaller  --  DEDICATED TO: LINUX / Raspberry Pi
#  Removes what the installer created: the application-menu entries, the virtualenv
#  (env/) and caches. Keeps your source files, config.ini and secrets.ini.
#  Run:   bash install/linux/uninstall.sh
# =============================================================================
set -u
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"   # UpdateManager/
cd "$APP_DIR"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "Removing menu entry ..."
APPS_DIR="$HOME/.local/share/applications"
# Current installs create only UpdateManager.desktop; the "(Debug)" one is a legacy entry from
# older versions and is removed too, so upgrading users are left clean.
rm -f "$APPS_DIR/UpdateManager.desktop" "$APPS_DIR/UpdateManager-Debug.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true

say "Removing virtualenv and caches ..."
rm -rf env img/.cache
find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

say "Done."
echo "  Kept: your source files, config.ini and secrets.ini."
echo "  To remove everything, delete this folder:  $APP_DIR"
echo "  (If the menu still shows it, log out/in — on Raspberry Pi OS: lxpanelctl restart)"
