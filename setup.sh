#!/usr/bin/env bash
# Bootstrap script for podcast_downloader on a brand new Mac.
#
# Install on any Mac with one command:
#   bash <(curl -fsSL https://raw.githubusercontent.com/Nomanriaz786/apple-scripts/main/setup.sh)
#
# After setup: bash run.sh

set -e

# ── Repo config — edit these two lines ───────────────────────────────────────
REPO_URL="https://github.com/Nomanriaz786/apple-scripts.git"
INSTALL_DIR="$HOME/podcast-downloader"         # ← where to clone (or leave as-is)
# ─────────────────────────────────────────────────────────────────────────────

echo "=== Podcast Downloader Setup ==="

# ── 1. macOS check ────────────────────────────────────────────────────────────
if [[ "$(uname)" != "Darwin" ]]; then
    echo "ERROR: This script only runs on macOS." && exit 1
fi

# ── 2. Git clone (skip if already inside the repo) ───────────────────────────
if [[ -f "$(dirname "$0")/scripts/podcast_downloader.py" ]]; then
    # Running from inside the repo already
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    echo "✓ Already inside repo at $SCRIPT_DIR"
else
    if [[ -d "$INSTALL_DIR/.git" ]]; then
        echo "✓ Repo already cloned at $INSTALL_DIR — pulling latest..."
        git -C "$INSTALL_DIR" pull --ff-only
    else
        echo "Cloning repo to $INSTALL_DIR ..."
        # If GITHUB_TOKEN is set, embed it in the URL for private repos
        if [[ -n "$GITHUB_TOKEN" ]]; then
            CLONE_URL="${REPO_URL/https:\/\//https:\/\/$GITHUB_TOKEN@}"
        else
            CLONE_URL="$REPO_URL"
        fi
        git clone "$CLONE_URL" "$INSTALL_DIR"
        echo "✓ Cloned to $INSTALL_DIR"
    fi
    SCRIPT_DIR="$INSTALL_DIR"
    cd "$SCRIPT_DIR"
fi

VENV_DIR="$SCRIPT_DIR/.venv"

# ── 3. Homebrew ───────────────────────────────────────────────────────────────
if ! command -v brew &>/dev/null; then
    echo "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv)"
else
    echo "✓ Homebrew already installed"
fi

# ── 4. Python 3.11 ────────────────────────────────────────────────────────────
if ! brew list python@3.11 &>/dev/null; then
    echo "Installing Python 3.11..."
    brew install python@3.11
else
    echo "✓ Python 3.11 already installed"
fi

PYTHON="$(brew --prefix python@3.11)/bin/python3.11"

# ── 5. Virtual environment ────────────────────────────────────────────────────
if [[ ! -d "$VENV_DIR" ]]; then
    echo "Creating virtual environment at .venv ..."
    "$PYTHON" -m venv "$VENV_DIR"
else
    echo "✓ Virtual environment already exists"
fi

source "$VENV_DIR/bin/activate"

# ── 6. Python packages ────────────────────────────────────────────────────────
echo "Installing Python packages..."
pip install --quiet --upgrade pip
pip install --quiet -r "$SCRIPT_DIR/requirements.txt"
echo "✓ Packages installed: pyobjc-framework-Quartz, mac-pyxa"

# ── 7. Required apps check ────────────────────────────────────────────────────
echo ""
echo "=== App Checklist ==="
check_app() {
    if [[ -d "/Applications/$1.app" || -d "$HOME/Applications/$1.app" ]]; then
        echo "✓ $1"
    else
        echo "✗ $1  ← INSTALL REQUIRED"
    fi
}
check_app "Google Chrome"
check_app "ProtonVPN"
echo "✓ Podcasts (built into macOS)"

# ── 8. macOS permissions reminder ─────────────────────────────────────────────
echo ""
echo "=== macOS Permissions Required ==="
echo "Open: System Settings → Privacy & Security"
echo ""
echo "  1. Accessibility → enable Terminal (or iTerm2)"
echo "     Required for AppleScript UI automation"
echo ""
echo "  2. Screen Recording → enable Terminal (or iTerm2)"
echo "     Required for Quartz pixel sampling"
echo ""
echo "  3. Automation → Terminal → Google Chrome, Podcasts, System Events"
echo "     Granted automatically on first run (click Allow when prompted)"
echo ""

# ── 9. input/tasks.json check ─────────────────────────────────────────────────
if [[ ! -f "$SCRIPT_DIR/input/tasks.json" ]]; then
    if [[ -f "$SCRIPT_DIR/input/tasks.json.example" ]]; then
        cp "$SCRIPT_DIR/input/tasks.json.example" "$SCRIPT_DIR/input/tasks.json"
        echo "✓ Created input/tasks.json from example — edit it before running"
    else
        echo "WARNING: input/tasks.json missing. Create it before running."
    fi
else
    echo "✓ input/tasks.json present"
fi

# ── 10. Create run.sh ─────────────────────────────────────────────────────────
RUN_SH="$SCRIPT_DIR/run.sh"
cat > "$RUN_SH" << 'RUN'
#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/.venv/bin/activate"
exec python3 "$SCRIPT_DIR/scripts/podcast_downloader.py" "$@"
RUN
chmod +x "$RUN_SH"
echo "✓ Created run.sh"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Location: $SCRIPT_DIR"
echo ""
echo "To run:"
echo "  cd $SCRIPT_DIR && bash run.sh"
echo ""
echo "Before first run:"
echo "  • Edit input/tasks.json with your podcast URLs and VPN settings"
echo "  • Open Google Chrome with Apple Podcasts show pages in the right tabs"
echo "  • Connect ProtonVPN manually once to confirm it works"
echo "  • Grant Accessibility + Screen Recording permissions (see above)"
