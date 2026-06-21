#!/bin/bash
# Double-click launcher for per-device ProtonVPN calibration.
# Measures this Mac's ProtonVPN "Connect" button geometry and writes it into
# input/tasks.json (vpn.calibration). Run this ONCE on each new Mac, then use
# run.command as normal.

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PY="$SCRIPT_DIR/.venv/bin/python"
CAL_SCRIPT="$SCRIPT_DIR/scripts/calibrate.py"

echo "ProtonVPN calibration"
echo "Project folder: $SCRIPT_DIR"
echo

if [ ! -x "$VENV_PY" ]; then
  echo "ERROR: project virtualenv not found at $VENV_PY"
  echo "Run run.command once first — it creates the .venv and installs deps."
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
