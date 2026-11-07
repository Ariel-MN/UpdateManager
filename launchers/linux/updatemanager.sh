#!/usr/bin/env bash
# =============================================================================
#  Update Manager launcher  --  DEDICATED TO: LINUX  (bash)
#  Runs `python3 updatemanager.py`. Uses the shared virtualenv at
#  <Crypto>/env if it exists, otherwise the system python3.
# =============================================================================
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"          # launchers/linux
APP_DIR="$(cd "$HERE/../.." && pwd)"           # UpdateManager/

# Prevent duplicate instances.
if pgrep -f "updatemanager.py" >/dev/null 2>&1; then
  exit 0
fi

if [ -f "$APP_DIR/../env/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$APP_DIR/../env/bin/activate"
fi

exec python3 "$APP_DIR/updatemanager.py"
