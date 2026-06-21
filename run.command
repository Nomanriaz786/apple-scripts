#!/bin/bash
# Double-click launcher for macOS.
# 1. Verifies python3 (3.10+).
# 2. Creates a project-local .venv on first run and installs deps into it.
# 3. Runs scripts/podcast_downloader.py.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INPUT_FILE="$SCRIPT_DIR/input/tasks.json"
PY_SCRIPT="$SCRIPT_DIR/scripts/podcast_downloader.py"
REQ_FILE="$SCRIPT_DIR/requirements.txt"
OUTPUT_DIR="$SCRIPT_DIR/logs"
STATE_FILE="$SCRIPT_DIR/state/runtime_state.json"
VENV_DIR="$SCRIPT_DIR/.venv"

echo "Apple Podcasts automation launcher"
echo "Project folder: $SCRIPT_DIR"
echo

# Sanity check the input/script files exist.
if [ ! -f "$PY_SCRIPT" ]; then
  echo "ERROR: automation script missing: $PY_SCRIPT"
  read -r -p "Press Enter to close..."
  exit 1
fi
if [ ! -f "$INPUT_FILE" ]; then
  echo "ERROR: input/tasks.json missing: $INPUT_FILE"
  read -r -p "Press Enter to close..."
  exit 1
fi

# Detect Python, create .venv, and install deps (shared with calibrate.command).
# Sets VENV_PY on success.
source "$SCRIPT_DIR/scripts/bootstrap_venv.sh"
if ! ensure_venv "$SCRIPT_DIR"; then
  echo
  read -r -p "Press Enter to close..."
  exit 1
fi

mkdir -p "$OUTPUT_DIR"
mkdir -p "$(dirname "$STATE_FILE")"

echo
echo "Starting real macOS UI automation."
echo "Do not move the mouse or keyboard while it runs."
echo

"$VENV_PY" "$PY_SCRIPT" \
  --input "$INPUT_FILE" \
  --state "$STATE_FILE" \
  --output-dir "$OUTPUT_DIR"
STATUS=$?

echo
if [ "$STATUS" -eq 0 ]; then
  echo "Automation finished successfully."
else
  echo "Automation finished with errors. Check the newest files in:"
  echo "  $OUTPUT_DIR"
  echo "And state/runtime_state.json for the failed step."
fi
echo
read -r -p "Press Enter to close..."
exit "$STATUS"
