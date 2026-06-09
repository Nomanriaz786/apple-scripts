#!/bin/bash
# Double-click launcher for macOS.
# 1. Verifies python3 (3.10+).
# 2. Creates a project-local .venv on first run and installs deps into it.
# 3. Runs scripts/podcast_downloader.py --execute.

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

# 1. Locate python3 (prefer Homebrew if installed; fall back to anything on PATH).
PYTHON_BIN=""
for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  echo "ERROR: python3 was not found."
  echo "Install Python 3.10 or newer. Easiest path:"
  echo "  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
  echo "  brew install python@3.11"
  echo
  read -r -p "Press Enter to close..."
  exit 1
fi

# 2. Verify Python version >= 3.10 (PyXA requirement).
PYVER=$("$PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYVER_OK=$("$PYTHON_BIN" -c "import sys; print(1 if sys.version_info >= (3,10) else 0)")
if [ "$PYVER_OK" != "1" ]; then
  echo "ERROR: Found Python $PYVER at $PYTHON_BIN."
  echo "PyXA requires Python 3.10 or newer."
  echo
  echo "Install a newer Python (Homebrew is easiest):"
  echo "  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
  echo "  brew install python@3.11"
  echo
  echo "After installing, open a fresh Terminal and double-click this file again."
  echo
  read -r -p "Press Enter to close..."
  exit 1
fi

echo "Using $PYTHON_BIN (Python $PYVER)"

# 3. Sanity check the input/script files exist.
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

# 4. Create project-local venv on first run. Sidesteps externally-managed-environment.
if [ ! -d "$VENV_DIR" ]; then
  echo "Creating virtual environment at .venv ..."
  "$PYTHON_BIN" -m venv "$VENV_DIR" || {
    echo "ERROR: failed to create virtual environment."
    read -r -p "Press Enter to close..."
    exit 1
  }
fi

VENV_PY="$VENV_DIR/bin/python3"

# 5. Install/refresh deps if PyXA is not importable.
if ! "$VENV_PY" -c "import PyXA" >/dev/null 2>&1; then
  echo "Installing dependencies into .venv ..."
  "$VENV_PY" -m pip install --upgrade pip
  if ! "$VENV_PY" -m pip install -r "$REQ_FILE"; then
    echo
    echo "ERROR: pip install failed. See the output above."
    read -r -p "Press Enter to close..."
    exit 1
  fi
fi

# 6. Verify deps are now importable.
if ! "$VENV_PY" -c "import PyXA" >/dev/null 2>&1; then
  echo "ERROR: PyXA still not importable after install."
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
