#!/usr/bin/env bash
# =============================================================================
#  Update Manager launcher  --  DEDICATED TO: macOS  (double-clickable .command)
#  First time only, make it executable so Finder can run it:
#     chmod +x UpdateManager.command
#  Uses a virtualenv if present: the repo-local env/ first, then a shared
#  <parent>/env; otherwise the system python3.
# =============================================================================
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"          # launchers/macos
APP_DIR="$(cd "$HERE/../.." && pwd)"           # UpdateManager/

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
