#!/usr/bin/env bash
# =============================================================================
#  Update Manager launcher  --  DEDICATED TO: macOS  (double-clickable .command)
#  First time only, make it executable so Finder can run it:
#     chmod +x UpdateManager.command
#  Uses the shared virtualenv at <Crypto>/env if present, else system python3.
# =============================================================================
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"          # launchers/macos
APP_DIR="$(cd "$HERE/../.." && pwd)"           # UpdateManager/

if [ -f "$APP_DIR/../env/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$APP_DIR/../env/bin/activate"
fi

exec python3 "$APP_DIR/updatemanager.py"
