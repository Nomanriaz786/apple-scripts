# Podcast Downloader v2 — Full Redesign Spec

**Date:** 2026-06-18  
**Goal:** Production-ready automation across 100+ Macs — deterministic state, zero external API dependency as success condition, targeted AX selectors, and complete README for setup and rollout.

---

## 1. Scope

Single file: `scripts/podcast_downloader.py` (~3000 lines).  
One PR, 7 commits. No new files except README updates and spec doc.  
`requirements.txt`: remove `certifi`; keep `mac-pyxa` as optional/dev only (guarded by try/except already).

---

## 2. Input JSON (minimal, unchanged)

```json
{
  "repeat": 2,
  "vpn": { "enabled": true, "app": "ProtonVPN", "location": "United States", "require_provider_in_org": false },
  "tabs": [{ "tab": 1, "videos": [1] }],
  "cleanup": true,
  "clean_start": false
}
```

`vpn` may also be `true` (shorthand: use ProtonVPN + US). Everything else (server names, slot routing, show URLs, episode counts) is auto-detected and saved in state.

---

## 3. State Schema Changes

Add to `runtime_state.json` under each cycle object:

```json
{
  "cycle_phases": { "1": { "vpn_state": "...", "download_state": "..." } },
  "processed_shows": {
    "1": [
      { "tab": 1, "url": "https://podcasts.apple.com/...", "show_name": "My Podcast", "videos_requested": [1], "videos_downloaded": [1] }
    ]
  },
  "vpn_verify_level": "tunnel+route",
  "download_state": "completed",
  "download_wait_seconds": 45,
  "cleanup_fallback_keyboard_used": false
}
```

`processed_shows[cycle]` is populated during the tab/download phase and consumed during cleanup.  
`vpn_verify_level` records how VPN was accepted (for per-machine diagnostics).

---

## 4. Commit Plan

### Commit 1 — Preflight + Config/State Schema

**Preflight checks** (run once at startup before any automation):
- Python ≥ 3.10
- `Google Chrome` process/app exists
- `Podcasts.app` exists at `/Applications/Podcasts.app`
- If `vpn.enabled`: `ProtonVPN.app` (or configured app) exists
- Accessibility permission: `AXIsProcessTrusted` via `osascript` test
- Chrome has at least `max(tab numbers in input)` open tabs
- `input/tasks.json` valid: `repeat` ≥ 1, `tabs` non-empty, `videos` all positive integers
- `state/` directory writable
- `logs/` directory writable

Each failed check logs clearly and exits with code 1. Pass → log "preflight_ok".

**State schema**: add `processed_shows`, `vpn_verify_level`, `download_state`, `download_wait_seconds`, `cleanup_fallback_keyboard_used` fields to `_default_state()`.

---

### Commit 2 — VPN Local-First Verification

Replace `_poll_verify` with a 4-level local-first strategy:

**Level 1 — tunnel_active**  
`ifconfig` output contains an active `utun` interface with `inet` address.

**Level 2 — tunnel+route** *(default success condition)*  
Level 1 + `route get default` output shows the default route has changed from baseline, OR `scutil --nwi` reports a tunnel interface is primary.

**Level 3 — tunnel+route+ip** *(optional)*  
Level 2 + public IP differs from baseline. Cached: only query once per verify cycle. On 429: mark `country_check=rate_limited`, still pass if Level 2 satisfied.

**Level 4 — tunnel+route+ip+country** *(optional, `strict_country_check=true`)*  
Level 3 + IP country == target. If API rate-limited and `require_provider_in_org=false`, degrade to Level 3.

**API backoff**: separate 60s cooldown per service (ip-api.com, ipinfo.io). A 429 on both services never causes VPN verification to fail if Level 2 is satisfied.

`_record_ip` updated to record `verify_level` alongside IP.

---

### Commit 3 — Chrome/Podcasts Tab Flow + Show Name Capture

`ChromeController.switch_tab`: unchanged structure, but now returns `(url, title)` and saves both to state.

`PodcastsController.open_url`: unchanged.

**Show name capture** (new method `_capture_show_name()`):  
After `wait_for_window`, AX-read `window 1`'s title or the first prominent heading element. Store as `show_name` in `processed_shows[cycle]` for this tab entry.  
Fallback: use the Chrome tab title (already captured in `switch_tab`).

---

### Commit 4 — See All + Episode Row Numbering/Download Improvements

`click_see_all`: no change to logic, but add `show_name` to log output so per-machine logs are readable.

`download_episode_row`:  
- Find target episode row by `video_no` (index into visible rows, 1-based)
- Read row `AXFrame` directly
- Hover row center (already done)
- Download click at `row_x + row_w - 35, row_cy` (relative to row frame, not window)
- If row AXFrame not found → log AX dump for that row and skip

---

### Commit 5 — Download Progress Detection (Stateful Wait)

Replace flat 45s wait with:

1. After all videos for a cycle clicked, call `wait_for_downloads_stable(max_wait=180)`:
   - Check `Downloaded` sidebar count via AX (look for badge/count change)
   - Check for `AXProgressIndicator` elements in episode rows (active download spinners)
   - Check `check_downloads_state()` (existing method) for `in_progress` / `completed`
   - Poll every 5s

2. State transitions:
   - `completed`: progress indicators gone, count stable → proceed
   - `in_progress`: keep polling up to `max_wait`
   - `stable_unknown`: no indicators found but can't confirm completion → wait 45s fallback, log `download_state=stable_unknown`
   - `timeout`: max_wait exceeded → log warning, proceed with cleanup anyway

3. Save to state: `download_state`, `download_wait_seconds` (actual wait used).

---

### Commit 6 — Cleanup by Show Name + Direct Remove… Selection

**Card finder** (`_find_downloaded_card_by_show_name(show_name)`):  
- BFS/targeted AX search for `static text` elements whose `AXValue` or `AXTitle` contains `show_name` (case-insensitive substring)
- Climb parent chain until element has size ≥ 80px and is in the content area (right of sidebar)
- That parent is the card

**Three-dots click** (`_find_more_button_in_card(card_elem)`):  
- Within card element, search for `button` whose `AXDescription` or `AXTitle` contains "more" (case-insensitive) OR whose `AXIdentifier` contains "more"
- Click it via AX `AXPress` action first; fallback to Quartz click at button frame center

**Remove… selection** (`_click_remove_menu_item()`):  
- After ⋯ click: search for `menu item` with `AXTitle` containing "Remove" or "Delete" (case-insensitive)
- `AXPress` it directly
- If no AX menu item found within 2s: fallback to keyboard `Down×3 + Enter`, log `cleanup_fallback_keyboard_used=true`

**Confirmation sheet**: unchanged (AX `sheet 1 of window 1` → click Remove/Delete button).

**Cleanup loop** entry: iterate `processed_shows[cycle]`, cleanup each show by name. Stop when no matching card found.

---

### Commit 7 — AX Diagnostics + README

**AX dump** (`_dump_ax_tree(label, root_elem=None, max_depth=6)`):  
- Triggered automatically when any AX selector returns no result
- Dumps to `logs/ax-dump-{label}-{timestamp}.txt`
- Format per element: `AXRole | AXTitle | AXDescription | AXValue | AXFrame | children_count`
- BFS limited to `max_depth=6` and 500 elements max (prevents multi-second freezes on deep trees)
- No screenshots taken anywhere in the codebase

**`--diagnose-ax` CLI flag**: runs a targeted AX dump of the current Podcasts window (Downloaded tab state) without executing any automation. Saves dump to logs. Exits after dump.

**README**: full rewrite covering all 19 sections specified by the user (Purpose, Supported macOS, Required apps, Zero-dependency design, Permissions, Input JSON, State behavior, Normal run, Dry run, Clean start, VPN levels, Download flow, Cleanup flow, Resume, Logs, Diagnostics, Limitations, 100-machine checklist, Troubleshooting).

---

## 5. Data Flow (Per Cycle)

```
preflight_ok
  → vpn_connect (if enabled)
      → local verify: Level 2 default
      → optional: Level 3/4 with API backoff
      → save vpn_verify_level to state
  → for each tab:
      → switch Chrome tab
      → save url + title to state
      → open in Podcasts
      → capture show_name → save to processed_shows[cycle]
      → click See All
      → scroll to top
      → for each video_no: download_episode_row
  → wait_for_downloads_stable
      → save download_state to state
  → if cleanup:
      → navigate_to_downloaded_tab
      → for each show in processed_shows[cycle]:
          → _find_downloaded_card_by_show_name
          → _find_more_button_in_card (AX first, Quartz fallback)
          → _click_remove_menu_item (AX first, keyboard fallback)
          → _click_confirmation_remove
  → quit Podcasts
  → mark cycle_completed
```

---

## 6. Resume Behavior

State keys checked on startup (existing logic preserved):
- `all_tabs_completed` → skip VPN+downloads, go to cleanup
- `cleanup_started` / `cleanup_completed` → skip or resume cleanup
- `cycle_completed` → skip entire cycle

New keys for fine-grained resume:
- `tab_N_completed` (per tab) → skip that tab's download phase
- `video_N_M_download_clicked` (per tab/video) → skip that video
- `downloads_stable` → skip wait phase, go straight to cleanup
- `cleanup_show_{show_name}_removed` → skip that show's cleanup

---

## 7. Dependencies

`requirements.txt` after this change:
```
mac-pyxa  # optional, dev/diagnostics only
```

All runtime code uses Python stdlib + `osascript` + macOS CLI tools (`ifconfig`, `scutil`, `route`, `networksetup`). No `certifi`. No `PyXA` in any required code path.

---

## 8. Test Plan

Execute in order (each test = `input/tasks.json` edit + `python scripts/podcast_downloader.py`):

1. `vpn=false, cleanup=false, repeat=1, tab 1, videos [1]` — baseline single download
2. `vpn=false, cleanup=false, repeat=1, tab 1, videos [1, 2]` — multi-video
3. `vpn=false, cleanup=false, repeat=1, tab 1, videos [8]` — high episode index
4. `vpn=false, cleanup=true, repeat=1, tab 1, videos [1]` — cleanup by show name
5. `vpn=false, cleanup=true, repeat=1, tabs [1, 2]` — multi-tab cleanup
6. `vpn=true, cleanup=false, repeat=1` — VPN local-first verify only
7. `vpn=true, cleanup=true, repeat=1` — full cycle
8. `vpn=true, cleanup=true, repeat=2` — rotation + cleanup across 2 cycles
9. Kill script mid-cycle, restart — resume from correct phase

Pass criteria: no hardcoded window coords used, no screenshot files created, AX dump emitted on any selector failure, `download_state` in state file reflects actual wait used, `cleanup_fallback_keyboard_used` logged when keyboard nav fires.

---

## 9. Out of Scope

- Selenium / Playwright / browser automation
- PyXA as required dependency
- Screenshot capture anywhere in the main flow
- Hardcoded pixel coords not derived from AX frames
