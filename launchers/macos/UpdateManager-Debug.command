#!/usr/bin/env bash
# =============================================================================
#  Update Manager DEBUG launcher  --  DEDICATED TO: macOS
#  Double-click opens Terminal and shows all output/errors; pauses at the end.
#  First time: chmod +x UpdateManager-Debug.command
# =============================================================================
set -u
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

echo "=== Update Manager (debug) ==="
echo "Python : $("$PY" --version 2>&1)  ($(command -v "$PY"))"
echo "App    : $APP_DIR/updatemanager.py"
echo "-------------------------------------------"
"$PY" "$APP_DIR/updatemanager.py"
code=$?
echo "-------------------------------------------"
echo "Exited with code $code"
read -r -p "Press Enter to close..." _
