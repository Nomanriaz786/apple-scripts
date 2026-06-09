# Podcast Downloader Refactor — Design

Date: 2026-06-09
Status: Approved, implementing directly

## Goal

Refactor the Apple Podcasts automation so it follows a minimal-input, state-driven workflow. User edits only 4 keys in `input/tasks.json`. Everything else is auto-discovered or derived, persisted to `state/runtime_state.json` between runs and after every meaningful step.

## Minimal Input

`input/tasks.json`:

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

- `repeat` — number of full cycles
- `vpn` — true/false, default false
- `tabs[].tab` — Chrome tab number (1-based)
- `tabs[].videos` — episode row numbers (1-based, counted after See All)
- `cleanup` — remove downloaded items from Library at end of cycle

No other user input. Podcast names, URLs, VPN server, app names, country, timeouts are all auto-discovered or have built-in defaults.

## State File

`state/runtime_state.json` — script-owned working memory, atomically written.

```json
{
  "current_cycle": 0,
  "completed_cycles": [],
  "current_tab": null,
  "current_video": null,
  "used_public_ips": [],
  "last_public_ip": null,
  "chrome_tabs_cache": {},
  "podcast_task_results": [],
  "cleanup_results": [],
  "see_all_state": {},
  "last_failed_step": null,
  "last_error": null,
  "resume_available": true,
  "started_at": "...",
  "updated_at": "..."
}
```

`completed_cycles` enables resume — already-completed cycles are skipped. `used_public_ips` enforces IP rotation across cycles when VPN is enabled.

## Folder Layout

```
apple-scripts/
├── README.md                   # all-in-one: install Python, deps, perms, run
├── requirements.txt            # pyxa, pyobjc-framework-ApplicationServices, pyobjc-framework-Cocoa
├── run.command                 # macOS double-click launcher
├── scripts/
│   └── podcast_downloader.py   # single file
├── input/
│   └── tasks.json              # minimal 4 keys
└── docs/superpowers/specs/
    └── 2026-06-09-podcast-downloader-refactor-design.md
```

`state/` and `logs/` are auto-created at first run.

## Single-File Class Structure

`scripts/podcast_downloader.py` organized as:

- `Config`, `TabTask` — validated input
- `StateManager` — atomic load/save of `runtime_state.json`
- `RunLogger` — `STEP NN | message` lines plus structured JSON report
- `NetworkState` — public IP fetch via `ipinfo.io`, tunnel interface detection via `ifconfig`
- `VPNController` — ProtonVPN connect, verify via IP geolocation, rotate IP across cycles
- `ChromeController` — activate, enumerate tabs (cache), switch tab, read URL
- `PodcastsController` — PyXA for app activate/quit/open + bounded osascript for UI
- `Orchestrator` — drives the workflow, persists state after each step

## Accessibility Strategy (Option C — Hybrid)

- **PyXA** for app lifecycle: `app.activate()`, `app.quit()`, plus `open -a Podcasts <url>` via subprocess.
- **Bounded osascript** for UI interactions: explicit iteration depth limit (4), wall-clock budget (5s), no recursion. Generic helpers `findButtonBounded`, `findButtonByDescBounded`, `collectRows` that callers parameterize.

## Bug Fixes

| Bug | Fix |
|---|---|
| VPN UI text false positive | IP geolocation + `ipinfo.io` org check. Already in code, preserved. |
| `See All` recursive hang | Bounded iterative search, 5s budget, depth 4. Returns `see_all_not_found` if not present. |
| Chrome JS path disabled | Primary path is `open -a Podcasts <url>`. Chrome JS click removed. |

## Workflow

```
load input → load/create state → validate environment
for cycle in 1..repeat:
    state.current_cycle = cycle
    if vpn:
        connect; verify via IP; ensure IP not in used_public_ips; append to used_public_ips
    activate Chrome
    state.chrome_tabs_cache = enumerate_all_tabs()
    for tab_task in tabs:
        switch Chrome tab; read URL; validate apple podcasts host
        open -a Podcasts <url>
        wait for Podcasts front window
        click See All (bounded) → status
        scroll to top
        for video in tab_task.videos:
            number rows top-to-bottom (scroll if needed)
            find row N
            click download / log status
            save per-video result to state
    if cleanup:
        open Downloaded sidebar
        loop: more button → Remove… → Remove from Library → wait
    quit Podcasts
    state.completed_cycles.append(cycle)
save final JSON report + plain log
```

## Video Row Statuses

- `download_clicked`
- `already_downloading`
- `already_downloaded`
- `download_control_not_found`
- `target_row_not_found`

## See All Statuses

- `clicked`
- `see_all_not_found`
- `list_already_expanded` (future — detected when >30 rows present without See All button)

## Cleanup Statuses

Per-iteration `result` in `cleanup_results`:

- `removed`
- `more_button_not_found` (terminates loop)
- `no_confirm_dialog`
- `no_more_items`
- `error: ...`

## Error Handling

On any exception:
- Persist `last_failed_step`, `last_error`, `current_tab`, `current_video` to state
- Log one short line: `STEP ERROR | <msg>`
- Save final JSON report including full final state snapshot
- Exit 1

Resume on next run reads `completed_cycles` and skips them.

## Logging Format

```
2026-06-09T14:33:00+05:00 | STEP 01 | Started podcast automation
2026-06-09T14:33:00+05:00 | STEP 02 | Loaded minimal input
2026-06-09T14:33:00+05:00 | STEP 03 | Loaded runtime state
2026-06-09T14:33:01+05:00 | STEP 04 | Detected Chrome tabs (3 found)
2026-06-09T14:33:01+05:00 | STEP 05 | Starting cycle 1
2026-06-09T14:33:01+05:00 | STEP 06 | VPN disabled
2026-06-09T14:33:01+05:00 | STEP 07 | Switching Chrome to tab 1
2026-06-09T14:33:01+05:00 | STEP 08 | Active tab URL detected
2026-06-09T14:33:02+05:00 | STEP 09 | Opening URL in Podcasts app
2026-06-09T14:33:04+05:00 | STEP 10 | Podcasts page loaded
2026-06-09T14:33:05+05:00 | STEP 11 | See All clicked
2026-06-09T14:33:06+05:00 | STEP 12 | Episode list reset to top
2026-06-09T14:33:07+05:00 | STEP 13 | Target video 1 found
2026-06-09T14:33:09+05:00 | STEP 14 | Target video 1 download_clicked
2026-06-09T14:33:10+05:00 | STEP 15 | Cycle 1 complete
2026-06-09T14:33:10+05:00 | STEP 16 | Final report saved
```

## Testing Plan

1. Windows dry-run for config + flow validation.
2. macOS: VPN off, one tab, `videos: [1]`.
3. macOS: `videos: [1, 2]`.
4. macOS: scroll case `videos: [8]`.
5. macOS: multiple tabs.
6. macOS: cleanup off then on.
7. macOS: VPN on, single cycle.
8. macOS: `repeat: 2` with VPN to verify IP rotation.
