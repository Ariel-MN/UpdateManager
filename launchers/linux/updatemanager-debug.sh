#!/usr/bin/env bash
# =============================================================================
#  Update Manager DEBUG launcher  --  DEDICATED TO: LINUX
#  Runs the app in THIS terminal so you can see all output and any traceback.
#  No duplicate-instance guard, output is not suppressed, and it pauses at the
#  end so the error stays readable. Run it from a terminal:
#     ./updatemanager-debug.sh
# =============================================================================
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"          # launchers/linux
APP_DIR="$(cd "$HERE/../.." && pwd)"           # UpdateManager/

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

echo "=== Update Manager (debug) ==="
echo "Python : $("$PY" --version 2>&1)  ($(command -v "$PY"))"
echo "App    : $APP_DIR/updatemanager.py"
echo "-------------------------------------------"
"$PY" "$APP_DIR/updatemanager.py"
code=$?
echo "-------------------------------------------"
echo "Exited with code $code"
read -r -p "Press Enter to close..." _
