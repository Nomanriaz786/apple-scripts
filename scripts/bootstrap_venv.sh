#!/bin/bash
# Shared virtualenv bootstrap for run.command and calibrate.command.
#
# Usage:
#   source "$SCRIPT_DIR/scripts/bootstrap_venv.sh"
#   ensure_venv "$SCRIPT_DIR" || { read -r -p "Press Enter to close..."; exit 1; }
#   # ... now use "$VENV_PY"
#
# On success: sets the global VENV_PY to the project venv's python and returns 0.
# On failure: prints an explanation and returns non-zero (caller handles the
# "Press Enter" prompt so double-click windows stay open).

ensure_venv() {
  local SCRIPT_DIR="$1"
  local REQ_FILE="$SCRIPT_DIR/requirements.txt"
  local VENV_DIR="$SCRIPT_DIR/.venv"

  # 1. Locate a Python >= 3.10. Probe versioned binaries first because
  #    python.org / Homebrew installs leave `python3` pointing at the Apple
  #    Command Line Tools (3.9) on Monterey.
  local PYTHON_BIN="" PYVER="" candidate ver_ok v
  local CANDIDATES="
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
    return 1
  fi

  echo "Using $PYTHON_BIN (Python $PYVER)"

  # 2. Create project-local venv on first run. Sidesteps externally-managed-environment.
  if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment at .venv ..."
    "$PYTHON_BIN" -m venv "$VENV_DIR" || {
      echo "ERROR: failed to create virtual environment."
      return 1
    }
  fi

  VENV_PY="$VENV_DIR/bin/python3"

  # 3. Install/refresh deps if PyXA is not importable.
  if ! "$VENV_PY" -c "import PyXA" >/dev/null 2>&1; then
    echo "Installing dependencies into .venv ..."
    "$VENV_PY" -m pip install --upgrade pip
    if ! "$VENV_PY" -m pip install -r "$REQ_FILE"; then
      echo
      echo "ERROR: pip install failed. See the output above."
      return 1
    fi
  fi

  # 4. Verify deps are now importable.
  if ! "$VENV_PY" -c "import PyXA" >/dev/null 2>&1; then
    echo "ERROR: PyXA still not importable after install."
    return 1
  fi

  return 0
}
