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

# 1. Locate a Python >= 3.10. Probe versioned binaries first because
#    python.org and Homebrew installs leave `python3` pointing at the
#    Apple Command Line Tools (3.9) on Monterey.
PYTHON_BIN=""
PYVER=""
CANDIDATES="
  /opt/homebrew/bin/python3.12
  /opt/homebrew/bin/python3.11
  /opt/homebrew/bin/python3.10
  /opt/homebrew/bin/python3
  /usr/local/bin/python3.12
  /usr/local/bin/python3.11
  /usr/local/bin/python3.10
  /usr/local/bin/python3
  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3
  /Library/Frameworks/Python.framework/Versions/3.11/bin/python3
  /Library/Frameworks/Python.framework/Versions/3.10/bin/python3
  python3.12
  python3.11
  python3.10
  python3
"

for candidate in $CANDIDATES; do
  if command -v "$candidate" >/dev/null 2>&1; then
    ver_ok=$("$candidate" -c "import sys; print(1 if sys.version_info >= (3,10) else 0)" 2>/dev/null)
    if [ "$ver_ok" = "1" ]; then
      PYTHON_BIN="$candidate"
      PYVER=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
      break
    fi
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  echo "ERROR: no Python 3.10+ found on this Mac."
  echo
  echo "Searched these paths:"
  for candidate in $CANDIDATES; do
    if command -v "$candidate" >/dev/null 2>&1; then
      v=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
      echo "  - $candidate (Python $v - too old)"
    fi
  done
  echo
  echo "Install Python 3.11 (any of these works):"
  echo "  - GUI installer:  https://www.python.org/downloads/macos/"
  echo "  - Homebrew:       brew install python@3.11"
  echo
  echo "After installing, double-click this file again."
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
