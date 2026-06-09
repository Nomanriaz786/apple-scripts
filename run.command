#!/bin/bash
# Double-click launcher for macOS.
# 1. Verifies python3 is installed.
# 2. Installs Python dependencies from requirements.txt if missing.
# 3. Runs scripts/podcast_downloader.py --execute against input/tasks.json.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INPUT_FILE="$SCRIPT_DIR/input/tasks.json"
PY_SCRIPT="$SCRIPT_DIR/scripts/podcast_downloader.py"
REQ_FILE="$SCRIPT_DIR/requirements.txt"
OUTPUT_DIR="$SCRIPT_DIR/logs"
STATE_FILE="$SCRIPT_DIR/state/runtime_state.json"

echo "Apple Podcasts automation launcher"
echo "Project folder: $SCRIPT_DIR"
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 was not found."
  echo "Install Python 3:"
  echo "  brew install python   (Homebrew)"
  echo "  or download from https://www.python.org/downloads/macos/"
  echo
  read -r -p "Press Enter to close..."
  exit 1
fi

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

# Make sure dependencies are present (PyXA, PyObjC). Idempotent on subsequent runs.
if [ -f "$REQ_FILE" ]; then
  echo "Checking Python dependencies..."
  if ! python3 -c "import PyXA" >/dev/null 2>&1; then
    echo "Installing dependencies from requirements.txt..."
    # Homebrew Python 3.11+ marks the environment as externally-managed.
    # Try the normal --user install first, fall back to --break-system-packages.
    if ! python3 -m pip install --user -r "$REQ_FILE" 2>/dev/null; then
      python3 -m pip install --user --break-system-packages -r "$REQ_FILE"
    fi
  fi
fi

mkdir -p "$OUTPUT_DIR"
mkdir -p "$(dirname "$STATE_FILE")"

echo
echo "Starting real macOS UI automation."
echo "Do not move the mouse or keyboard while it runs."
echo

python3 "$PY_SCRIPT" \
  --input "$INPUT_FILE" \
  --state "$STATE_FILE" \
  --output-dir "$OUTPUT_DIR" \
  --execute
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
