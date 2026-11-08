#!/usr/bin/env bash
# =============================================================================
#  Update Manager launcher  --  DEDICATED TO: LINUX  (bash)
#  Runs updatemanager.py using a virtualenv if one exists: the repo-local env/
#  first, then a shared <parent>/env; otherwise the system python3.
# =============================================================================
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"          # launchers/linux
APP_DIR="$(cd "$HERE/../.." && pwd)"           # UpdateManager/

# Prevent duplicate instances.
if pgrep -f "[u]pdatemanager.py" >/dev/null 2>&1; then
  exit 0
fi

# Activate a virtualenv if present (repo-local env/ first, then a shared ../env).
PY="python3"
for VENV in "$APP_DIR/env" "$APP_DIR/../env"; do
  if [ -f "$VENV/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
    PY="python"
    break
  fi
done

exec "$PY" "$APP_DIR/updatemanager.py"
