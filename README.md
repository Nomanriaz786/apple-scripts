# Apple Podcasts Automation

A macOS automation script that opens Apple Podcasts URLs from your open Chrome tabs, clicks `See All`, and downloads the episode rows you ask for. Optional VPN gate, optional cleanup that removes the downloaded items afterward.

You only edit one small file (`input/tasks.json`). The script auto-discovers Chrome tabs, podcast pages, public IP state, and remembers everything in `state/runtime_state.json` between runs.

---

## What's in this folder

```text
apple-scripts/
├── README.md                # this file
├── requirements.txt         # Python deps (PyXA, PyObjC)
├── run.command              # double-click launcher for macOS
├── scripts/
│   └── podcast_downloader.py
├── input/
│   └── tasks.json           # your config (4 keys)
└── docs/                    # design reference (optional)
```

`state/` and `logs/` are created automatically on first run.

---

## Setup on a fresh Mac (one-time)

### 1. Install Python 3.10 or newer

**Important:** the Python that ships with macOS Command Line Tools (`/Library/Developer/CommandLineTools/usr/bin/python3`) is usually Python 3.9, which is **too old** for PyXA. You need 3.10+.

Easiest way — Homebrew:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install python@3.11
```

After the Homebrew install finishes, follow its instructions to add `/opt/homebrew/bin` to your PATH (it prints the exact commands). Open a fresh Terminal window after.

Or download the official installer: <https://www.python.org/downloads/macos/> (pick 3.11 or 3.12).

Verify:

```bash
python3 --version
```

You should see `Python 3.10` or newer. If it still says 3.9, your PATH is pointing at the Apple CLT Python — open a fresh Terminal or re-check the Homebrew PATH setup.

### 2. Dependencies (handled automatically)

You do **not** install Python packages globally. The first run of `run.command` creates a project-local `.venv/` folder and installs `PyXA` + `PyObjC` into it. This sidesteps macOS's "externally-managed environment" protection and keeps your system Python clean.

If you prefer to do it manually instead of relying on the launcher:

```bash
cd /path/to/apple-scripts
python3 -m venv .venv
.venv/bin/python3 -m pip install -r requirements.txt
```

### 3. Grant Accessibility and Automation permission

The script controls Chrome, the Podcasts app, and (optionally) Proton VPN through macOS UI scripting. macOS requires you to allow this once.

Open **System Settings** → **Privacy & Security** → **Accessibility**, then add and enable:

- **Terminal** (or **iTerm**, whichever you launch the script from)

If you double-click `run.command` from Finder, also enable:

- **Finder**
- **bash** or **zsh** (the shell that ran the launcher)

When the script first tries to control Chrome or Podcasts, macOS may pop a prompt asking you to allow it. Click **OK**.

If those prompts don't appear, add the same apps under **Privacy & Security** → **Automation** and tick the destination apps (`Google Chrome`, `Podcasts`, `System Events`, and `Proton VPN` if used).

### 4. Prepare Chrome

Open Google Chrome and load your Apple Podcasts show pages in tabs in the exact order you'll reference in `input/tasks.json`. Tab 1 is the leftmost tab.

### 5. (Optional) Install Proton VPN

Only if you set `"vpn": true` in `input/tasks.json`. Install Proton VPN from <https://protonvpn.com/download/> and sign in once so it stays signed in.

---

## Configure: edit `input/tasks.json`

The whole config:

```json
{
  "repeat": 1,
  "vpn": false,
  "tabs": [
    { "tab": 1, "videos": [1] }
  ],
  "cleanup": false
}
```

| Key | Meaning |
| --- | --- |
| `repeat` | Number of full cycles to run. Each cycle re-opens every tab and downloads the listed videos. |
| `vpn` | `true` to require Proton VPN to be connected to the US before each cycle. `false` to skip. |
| `vpn_servers` | Optional. List of exact Proton server names (e.g. `"US-AZ#81"`). When present, cycle N connects to server `[(N-1) % len]`. When omitted, Quick Connect is used with IP-based de-duplication. |
| `tabs[].tab` | Chrome tab number (1 = leftmost). |
| `tabs[].videos` | Episode row numbers to download, counted from top after `See All`. |
| `cleanup` | `true` to remove the downloaded items from the Library at the end of each cycle. |

Examples:

```json
{ "repeat": 1, "vpn": false, "tabs": [{ "tab": 1, "videos": [1, 2, 3] }], "cleanup": false }
```

```json
{
  "repeat": 2,
  "vpn": true,
  "tabs": [
    { "tab": 1, "videos": [1, 4] },
    { "tab": 3, "videos": [8] }
  ],
  "cleanup": true
}
```

Deterministic per-server VPN rotation:

```json
{
  "repeat": 5,
  "vpn": true,
  "vpn_servers": ["US-AZ#81", "US-AZ#82", "US-AZ#83", "US-AZ#84", "US-AZ#85"],
  "tabs": [
    { "tab": 1, "videos": [1] }
  ],
  "cleanup": true
}
```

How to populate `vpn_servers`: open Proton VPN, type `united` in the country search, click the chevron next to **United States** to expand the server list, and copy the names you want (e.g. `US-AZ#81`). Use them verbatim — the script clicks rows whose visible name contains that string. Cycles wrap around the list (`repeat: 7` with 3 servers reuses `[0, 1, 2, 0, 1, 2, 0]`).

---

## Run

### Option A — Double-click `run.command`

In Finder, open the project folder and double-click **`run.command`**. A Terminal window opens, runs the automation, and prints status. Press Enter to close at the end.

If macOS blocks it with "cannot be opened because it is from an unidentified developer", right-click the file → **Open** → **Open** in the confirmation dialog. You only need to do this once.

### Option B — Command line

```bash
cd /path/to/apple-scripts
.venv/bin/python3 scripts/podcast_downloader.py
```

---

## Where things land after a run

| Path | What's in it |
| --- | --- |
| `logs/podcast-download-YYYYMMDD-HHMMSS.log` | Human-readable step-by-step trace |
| `logs/podcast-download-YYYYMMDD-HHMMSS.json` | Full structured report including final state |
| `state/runtime_state.json` | Persistent working memory: current cycle, used IPs, Chrome tab cache, per-video results, last failed step |

---

## Reading the logs

Each line is one short step:

```text
2026-06-09T14:33:00+05:00 | STEP 01 | Started podcast automation
2026-06-09T14:33:01+05:00 | STEP 04 | Detected Chrome tabs (3 found)
2026-06-09T14:33:01+05:00 | STEP 05 | Starting cycle 1
2026-06-09T14:33:01+05:00 | STEP 06 | VPN disabled
2026-06-09T14:33:02+05:00 | STEP 07 | Switching Chrome to tab 1
2026-06-09T14:33:03+05:00 | STEP 09 | Opening URL in Podcasts app
2026-06-09T14:33:05+05:00 | STEP 11 | See All clicked
2026-06-09T14:33:07+05:00 | STEP 13 | Target video 1 found
2026-06-09T14:33:08+05:00 | STEP 14 | Target video 1 download_clicked
2026-06-09T14:33:10+05:00 | STEP 15 | Cycle 1 complete
```

Per-video statuses:

- `download_clicked` — clicked the download button
- `already_downloading` — already in progress, skipped
- `already_downloaded` — already in the Library
- `download_control_not_found` — could not find a Download button in that row
- `target_row_not_found` — row number is past the end of the visible list

---

## Common failures

**`PyXA not installed`** (or `Found Python 3.9 ... requires 3.10+`)
Your Python is too old or not the one the venv was built against. Install Homebrew Python 3.11 (see step 1), delete the `.venv/` folder, and double-click `run.command` again.

```bash
brew install python@3.11
rm -rf .venv
./run.command
```

**`See All button not found`**
The Podcasts page is an individual episode page (URL contains `?i=`) or didn't finish loading. Open the same URL in Podcasts manually to confirm it shows the full episode list, then re-run.

**`Tab N is not an Apple Podcasts URL`**
That Chrome tab isn't on `podcasts.apple.com`. Re-arrange your tabs to match `tabs[].tab` in the config.

**`VPN verification failed`**
Proton VPN UI shows a Connect button but the public IP doesn't match the requested country/Proton org within 30 seconds. Try connecting Proton manually in the country you want, then run again.

**`osascript failed: ... not allowed assistive access`**
You haven't granted Accessibility permission yet. Re-do step 3 of setup.

---

## Resume

If a run fails partway, the next run reads `state/runtime_state.json` and skips any cycle in `completed_cycles`. To start fresh, delete `state/runtime_state.json` (or just the `completed_cycles` array inside it).

---

## Notes

- This is a personal-library automation. It opens shows and downloads episodes you already use Apple Podcasts to listen to. Don't use it to drive fake engagement or violate Apple's terms.
- VPN support is optional and intended for approved region/network testing only.
- No screenshots, no image recognition. All UI control is through macOS Accessibility (osascript + PyXA).
