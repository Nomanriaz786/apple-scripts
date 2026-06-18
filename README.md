# Apple Podcasts Automation

macOS automation that opens Apple Podcasts URLs from Chrome tabs, clicks See All, downloads specified episode rows, and optionally removes them afterward. Designed for reliable operation across 100+ Macs.

---

## 1. Purpose

Automate repeating download-and-cleanup cycles in the macOS Podcasts app with minimal configuration. The script controls native macOS UI — no browser automation, no Selenium.

---

## 2. Supported macOS Versions

macOS 12 Monterey and later. Tested on macOS 13 Ventura and 14 Sonoma.

---

## 3. Required Apps

| App | Required | Notes |
|-----|----------|-------|
| Google Chrome | Yes | Must be open with Podcasts tabs loaded |
| Apple Podcasts | Yes | Pre-installed on macOS |
| ProtonVPN | Only if `vpn.enabled: true` | Must be signed in |
| Python 3.9+ | Yes | `python3 --version` to check |

---

## 4. Zero-Dependency Runtime Design

The production script uses **Python standard library only** plus built-in macOS tools:

- `osascript` / AppleScript / System Events
- `ifconfig`, `scutil`, `route` — VPN verification
- `open` — launch apps
- `subprocess`, `urllib` — all stdlib

`mac-pyxa` in `requirements.txt` is **optional**. The script works without it. Install it only for development or diagnostics.

```bash
pip3 install -r requirements.txt   # optional — PyXA only
```

---

## 5. Required macOS Permissions

Before first run, grant these in **System Settings → Privacy & Security**:

| Permission | Who needs it | Why |
|-----------|-------------|-----|
| **Accessibility** | Terminal (or your launcher) | Required — script controls Podcasts and Chrome UI |
| **Automation** | Terminal | Required — `tell application "Podcasts"` |
| Screen Recording | Not required | Screenshots are disabled by default |

To verify Accessibility is granted:
```bash
python3 -c "import subprocess; r = subprocess.run(['osascript', '-e', 'tell application \"System Events\" to tell process \"Finder\" to get exists'], capture_output=True); print('OK' if r.returncode == 0 else r.stderr.decode())"
```

---

## 6. Input JSON Format

Edit `input/tasks.json`. Only these keys are used:

```json
{
  "repeat": 2,
  "vpn": {
    "enabled": true,
    "app": "ProtonVPN",
    "location": "United States",
    "require_provider_in_org": false
  },
  "tabs": [
    { "tab": 1, "videos": [1] },
    { "tab": 2, "videos": [1, 3] }
  ],
  "cleanup": true,
  "clean_start": false
}
```

| Key | Type | Description |
|-----|------|-------------|
| `repeat` | int ≥ 1 | Number of full cycles to run |
| `vpn` | object or `false` | VPN settings. Set to `false` to disable |
| `vpn.enabled` | bool | Enable VPN gate |
| `vpn.app` | string | VPN app name (default: `"ProtonVPN"`) |
| `vpn.location` | string | Target location (default: `"United States"`) |
| `vpn.require_provider_in_org` | bool | Require VPN provider name in IP org field |
| `tabs` | array | Chrome tab numbers and episode row indexes to download |
| `tabs[].tab` | int ≥ 1 | Chrome tab number (1-based, in front window) |
| `tabs[].videos` | int[] | Episode row numbers to download (1 = top row) |
| `cleanup` | bool | Remove downloaded items after each cycle |
| `clean_start` | bool | Remove any stale downloads before cycle 1 starts |

Everything else (server names, show URLs, VPN servers) is auto-detected and saved in state.

---

## 7. State File Behavior

`state/runtime_state.json` is created automatically and updated after every action.

Key fields:

| Field | Meaning |
|-------|---------|
| `completed_cycles` | Cycle numbers that finished successfully |
| `processed_shows` | Per-cycle show names captured during tab processing |
| `vpn_verify_level` | How VPN was accepted: `tunnel+route`, `tunnel+route+ip`, etc. |
| `download_state` | `completed`, `stable_unknown`, or `timeout` |
| `download_wait_seconds` | Actual seconds waited for downloads |
| `cleanup_fallback_keyboard_used` | `true` if keyboard nav was used instead of AX Remove |
| `cycle_phases` | Timestamped phase checkpoints per cycle for resume |
| `last_failed_step` | Step number of last failure |
| `last_error` | Error message of last failure |

Delete `state/runtime_state.json` to start fresh. The script resumes from the last checkpoint if the file exists.

---

## 8. Normal Run

```bash
cd /path/to/apple-scripts
python3 scripts/podcast_downloader.py
```

Or double-click `run.command`.

---

## 9. Dry Run / Diagnostics

```bash
# Check VPN state only (no connection, no download)
python3 scripts/podcast_downloader.py --diagnose-vpn

# Check apps, Chrome tabs, VPN state, Podcasts UI
python3 scripts/podcast_downloader.py --diagnose-live

# Dump Podcasts Accessibility tree to logs/ (no automation)
python3 scripts/podcast_downloader.py --diagnose-ax

# Connect VPN only (no Chrome or Podcasts)
python3 scripts/podcast_downloader.py --test-vpn-connect
```

---

## 10. Clean Start Behavior

Set `"clean_start": true` in `input/tasks.json` to remove any stale downloaded items **before** cycle 1 begins. Use this when a previous run crashed mid-cleanup.

This runs one extra pass through the Downloaded tab using the generic card cleanup before any new downloads start.

---

## 11. VPN Verification Levels

VPN is verified locally first — **never fails due to API rate limits alone**.

| Level | Condition | Default? |
|-------|-----------|---------|
| `tunnel+route` | Active utun interface + default route changed | **Yes — minimum for "connected"** |
| `tunnel+route+ip` | Level 2 + public IP different from baseline | Optional |
| `tunnel+route+ip+country` | Level 3 + IP country matches target | Optional |
| `tunnel_only` | Legacy tunnel-only (pre-v2) | No |

Public IP checks (ip-api.com → ipinfo.io fallback) are attempted opportunistically. If both return 429, the script accepts the Level 2 (local) result and logs `vpn_verify_level=tunnel+route`.

Set `"require_provider_in_org": false` to skip org/provider name verification. Useful on shared or business networks.

---

## 12. Download Flow

Per cycle, per tab:

1. Switch Chrome to tab N
2. Read URL and title; save to state
3. Open URL in Podcasts; wait for page render (10s + `wait_for_window`)
4. **Capture show name** from window title / AX heading → save to `processed_shows[cycle]`
5. Click See All (AX BFS; accepts list_already_expanded)
6. Scroll episode list to top
7. For each video number: hover row center → click download icon at `more_x - 35`

Download positions are derived from episode row AXFrame. No hardcoded window coordinates.

---

## 13. Cleanup Flow

Cleanup runs **once per cycle after all tabs complete and downloads are stable**.

**Download wait:**
1. Look for `AXProgressIndicator` elements in Podcasts window
2. Look for "Downloading" text in episode rows
3. If neither found: wait 45s fallback, log `download_state=stable_unknown`
4. If active: poll every 5s up to 180s max

**Card removal (per show, by name):**
1. Navigate to Downloaded sidebar tab
2. BFS for text element containing show name (case-insensitive)
3. Climb parent chain to card container (≥80px, in content area)
4. Activate Podcasts → Quartz hover + click ⋯ at card bottom-right
5. **AX first**: look for menu item containing "Remove" or "Delete" in floating AXMenu windows
6. **Keyboard fallback** (logged as `cleanup_fallback_keyboard_used=true`): Down×3 + Enter
7. AX confirmation sheet → click Remove button

If show names were not captured (resume from crash), falls back to generic card detection.

---

## 14. Resume After Crash

The script resumes from the last completed phase checkpoint on restart.

Resume logic:

| State in `cycle_phases` | Action on restart |
|------------------------|------------------|
| `all_tabs_completed` | Skip VPN + downloads, go straight to cleanup |
| `cleanup_started` but not `cleanup_completed` | Resume cleanup |
| `cycle_completed` | Skip entire cycle |
| `tab_N_completed` | Skip that tab's download |
| `video_N_M_download_clicked` | Skip that video |
| `downloads_stable` | Skip download wait |

To force a full re-run: delete `state/runtime_state.json`.

---

## 15. Logs and Reports

Each run creates timestamped files in `logs/`:

| File | Contents |
|------|----------|
| `podcast-download-YYYYMMDD-HHMMSS.log` | Per-step log lines |
| `podcast-download-YYYYMMDD-HHMMSS.json` | Full event log + final state |
| `ax-dump-*.txt` | AX tree dump (created when a selector fails) |

AX dump format (one line per element):
```
depth+role | title | description | value (truncated at 80 chars) | x,y,w,h | child_count
```

---

## 16. Diagnostics Mode

When any AX selector returns no result, the script automatically saves an AX dump:
```
logs/ax-dump-cleanup_card_not_found_My_Show-20260618-143000.txt
```

Run explicitly:
```bash
python3 scripts/podcast_downloader.py --diagnose-ax
```

This activates Podcasts, navigates to the Downloaded tab, dumps the full AX tree (up to depth 8, 1000 elements), and exits. No automation is performed.

**No screenshots are taken anywhere in the script.** All debugging is done via AX dumps and structured log files.

---

## 17. Known Limitations

- **Mac Catalyst context menus**: The ⋯ menu in the Downloaded card is not AX-accessible as a standard menu. AX Remove selection is attempted first; keyboard Down×3+Enter is the fallback. When keyboard is used, `cleanup_fallback_keyboard_used=true` is logged in state.
- **Show name capture**: Relies on Podcasts window title or first AX static text in content area. If the title is missing or wrong, cleanup falls back to generic card detection.
- **VPN slot reliability**: Some ProtonVPN server slots may time out during connection setup. The script tries all available slots before giving up.
- **AXProgressIndicator**: If Podcasts does not expose progress indicators in the AX tree, download wait falls back to 45s flat wait (`stable_unknown`).

---

## 18. 100-Machine Rollout Checklist

Run these steps on **3 Macs** before deploying to 100+:

- [ ] macOS version ≥ 12 on all target Macs
- [ ] Python 3.9+ installed (`python3 --version`)
- [ ] Accessibility permission granted for Terminal (or launcher)
- [ ] Automation permission granted for Terminal
- [ ] Google Chrome open with Apple Podcasts tabs in position
- [ ] ProtonVPN installed and signed in (if `vpn.enabled: true`)
- [ ] `input/tasks.json` deployed to each Mac
- [ ] `state/` and `logs/` directories writable by the running user
- [ ] Run test sequence 1–5 (vpn=false) on each Mac before enabling VPN
- [ ] Check `logs/ax-dump-*.txt` files — if present, selector calibration needed
- [ ] Check `state/runtime_state.json` for `cleanup_fallback_keyboard_used: true` — means AX Remove not working on that Mac
- [ ] Verify `vpn_verify_level` in state is `tunnel+route` or better — never empty

Test sequence (in order):

```
1. vpn=false, cleanup=false, repeat=1, tab 1, videos [1]
2. vpn=false, cleanup=false, repeat=1, tab 1, videos [1, 2]
3. vpn=false, cleanup=false, repeat=1, tab 1, videos [8]
4. vpn=false, cleanup=true,  repeat=1, tab 1, videos [1]
5. vpn=false, cleanup=true,  repeat=1, multiple tabs
6. vpn=true,  cleanup=false, repeat=1
7. vpn=true,  cleanup=true,  repeat=1
8. vpn=true,  cleanup=true,  repeat=2
9. Kill script mid-cycle, restart — verify resume from correct phase
```

---

## 19. Troubleshooting

**`Accessibility permission not granted`**
→ System Settings → Privacy & Security → Accessibility → enable Terminal

**`Required app not found: ProtonVPN`**
→ Install ProtonVPN from the App Store or protonvpn.com. Sign in before running.

**`Configured Chrome tab N was not found`**
→ Chrome must have at least N tabs open in its front window before you run the script.

**`See All not found`**
→ The Podcasts page may not have loaded. The script waits 10s after `open`. If the show page is slow, increase the `time.sleep(10)` in `_process_tab`.

**`card_not_found` in cleanup**
→ Check `logs/ax-dump-cleanup_card_not_found_*.txt` for the AX tree at the time of failure. Run `--diagnose-ax` to get a fresh dump.

**`vpn_verify_level` is empty in state**
→ VPN verification timed out. Check ProtonVPN is connected, try `--test-vpn-connect` to isolate.

**`cleanup_fallback_keyboard_used: true`**
→ Normal — Mac Catalyst menus are not always AX-accessible. The keyboard fallback works correctly. If cleanup is still failing, check `ax-dump` files.

**`download_state: stable_unknown`**
→ The script could not detect active downloads (no AXProgressIndicator in AX tree). A 45s fallback wait was used. This is safe but may be too short for slow connections — increase the fallback if needed.
