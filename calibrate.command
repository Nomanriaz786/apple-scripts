#!/bin/bash
# Double-click launcher for per-device ProtonVPN calibration.
# Measures this Mac's ProtonVPN "Connect" button geometry and writes it into
# input/tasks.json (vpn.calibration). Run this ONCE on each new Mac, then use
# run.command as normal.

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CAL_SCRIPT="$SCRIPT_DIR/scripts/calibrate.py"

echo "ProtonVPN calibration"
echo "Project folder: $SCRIPT_DIR"
echo

# Detect Python, create .venv, and install deps (shared with run.command).
# Sets VENV_PY on success — so calibrate works on a fresh Mac without needing
# run.command to have been run first.
source "$SCRIPT_DIR/scripts/bootstrap_venv.sh"
if ! ensure_venv "$SCRIPT_DIR"; then
  echo
  read -r -p "Press Enter to close..."
  exit 1
fi

"$VENV_PY" "$CAL_SCRIPT"
STATUS=$?

echo
if [ "$STATUS" -eq 0 ]; then
  echo "Calibration complete. You can now run run.command."
else
  echo "Calibration did not finish. See the messages above."
fi
read -r -p "Press Enter to close..."
exit "$STATUS"
