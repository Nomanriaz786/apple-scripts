#!/usr/bin/env python3
"""Per-device ProtonVPN calibration.

The main automation clicks ProtonVPN's per-server "Connect" button, which is
drawn only on hover and never exposed to Accessibility — so its position has to
be hit by pixel coordinates.  Those coordinates differ across machines, displays
and ProtonVPN versions.  This tool measures them ONCE on a given Mac and writes
them into input/tasks.json (under vpn.calibration); the main script then reads
them and connects correctly on that device.

What it measures (all anchored to values the main script reads live at runtime,
so they survive the window being moved):
  • connect_offset_from_right = window_right_edge − Connect_button_x
  • row_height                = vertical gap between two server rows
  • header_height             = US country-header row height

How it works: it searches "United States" and expands the list for you, then
asks you to hover the Connect button of the 1st and 2nd US servers.  It captures
each position automatically when your cursor holds still — no clicking, no
typing while you hover.

Run:  python3 scripts/calibrate.py
  or: double-click calibrate.command
"""
from __future__ import annotations
import json, subprocess, sys, time, re
from pathlib import Path

try:
    import Quartz  # type: ignore
except ImportError:
    print("ERROR: pyobjc/Quartz not available. Run via the project's .venv "
          "(the same Python that runs the main script).")
    sys.exit(1)

PROJECT = Path(__file__).resolve().parent.parent
TASKS = PROJECT / "input" / "tasks.json"
APP_PROCESS = "ProtonVPN"
LOCATION = "United States"


def osa(script: str, timeout: int = 20) -> str:
    p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip())
    return p.stdout.strip()


def nums(s: str) -> list[int]:
    return [int(v) for v in re.findall(r"-?\d+", s)]


def _mouse(kind, x, y):
    ev = Quartz.CGEventCreateMouseEvent(None, kind, Quartz.CGPoint(x=float(x), y=float(y)),
                                        Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
    time.sleep(0.05)


def _key(vk, down, flags=0):
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateCombinedSessionState)
    ev = Quartz.CGEventCreateKeyboardEvent(src, vk, down)
    if flags:
        Quartz.CGEventSetFlags(ev, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
    time.sleep(0.05)


def cursor_xy() -> tuple[int, int]:
    loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return int(loc.x), int(loc.y)


def capture_when_still(label: str, timeout: float = 30.0, still_secs: float = 1.5,
                       tol: int = 2) -> tuple[int, int]:
    """Wait for the cursor to stop moving (user is hovering the target), then
    return its position. No keypress needed, so ProtonVPN keeps focus/hover."""
    print(f"  → Hover the {label} and HOLD STILL… (capturing when your mouse stops)", flush=True)
    last = None
    still_since = None
    t0 = time.time()
    while time.time() - t0 < timeout:
        x, y = cursor_xy()
        if last and abs(x - last[0]) <= tol and abs(y - last[1]) <= tol:
            if still_since is None:
                still_since = time.time()
            elif time.time() - still_since >= still_secs:
                print(f"    captured {label} at ({x},{y})", flush=True)
                return x, y
        else:
            still_since = None
        last = (x, y)
        time.sleep(0.1)
    raise TimeoutError(f"Did not detect a steady hover on the {label} within {int(timeout)}s")


def ensure_searched_and_expanded() -> tuple[int, int, int, int]:
    """Focus ProtonVPN, paste 'United States', read window + header-row position,
    then expand the country. Returns (w_x, w_y, w_w, r2_top)."""
    # Focus app + search field via Accessibility (no coordinates needed).
    osa(f'''tell application "System Events" to tell process "{APP_PROCESS}"
      set frontmost to true
      delay 0.6
      if not (exists window 1) then error "ProtonVPN window not found"
      set focused of (text field 1 of group 1 of window 1) to true
    end tell''')
    # Paste the country filter via clipboard (keystroke injection is swallowed).
    old = subprocess.run(["pbpaste"], capture_output=True).stdout
    try:
        subprocess.run(["pbcopy"], input=LOCATION.encode(), check=True)
        _key(0x00, True, Quartz.kCGEventFlagMaskCommand); _key(0x00, False, Quartz.kCGEventFlagMaskCommand)
        time.sleep(0.1)
        _key(0x33, True); _key(0x33, False); time.sleep(0.4)
        _key(0x09, True, Quartz.kCGEventFlagMaskCommand); _key(0x09, False, Quartz.kCGEventFlagMaskCommand)
        time.sleep(1.5)
    finally:
        subprocess.run(["pbcopy"], input=old, check=False)

    # Read window + header row (row 2) — collapsed list has only ~2 rows so AX is fast.
    info = osa(f'''tell application "System Events" to tell process "{APP_PROCESS}"
      set sc to scroll area 1 of window 1
      try
        set value of scroll bar 1 of sc to 0
      end try
      set tbl to table 1 of sc
      set rp to position of row 2 of tbl
      set wp to position of window 1
      set ws to size of window 1
      return "" & (item 1 of wp) & "," & (item 2 of wp) & "," & (item 1 of ws) & "," & (item 2 of ws) & "," & (item 2 of rp)
    end tell''')
    w_x, w_y, w_w, w_h, r2_top = nums(info)[:5]

    # Expand the US country row (click its centre) so individual servers appear.
    ex_x, ex_y = w_x + w_w // 2, r2_top + 24
    _mouse(Quartz.kCGEventLeftMouseDown, ex_x, ex_y)
    _mouse(Quartz.kCGEventLeftMouseUp, ex_x, ex_y)
    time.sleep(1.2)
    return w_x, w_y, w_w, r2_top


def write_calibration(values: dict) -> None:
    raw = json.loads(TASKS.read_text(encoding="utf-8-sig"))
    vpn = raw.get("vpn")
    if not isinstance(vpn, dict):
        vpn = {"enabled": True} if vpn in (True, None) else {"enabled": bool(vpn)}
        raw["vpn"] = vpn
    vpn["calibration"] = values
    backup = TASKS.with_suffix(".json.bak")
    backup.write_text(TASKS.read_text(encoding="utf-8-sig"), encoding="utf-8")
    TASKS.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote vpn.calibration to {TASKS}")
    print(f"(backup of the previous file saved to {backup})")


def main() -> int:
    print(__doc__)
    print("=" * 70)
    print("Before you start:")
    print("  1. Open ProtonVPN and make sure you are SIGNED IN and DISCONNECTED.")
    print("  2. Leave the ProtonVPN window visible on screen.")
    print("  3. Do not touch the keyboard/mouse until asked to hover.")
    input("\nPress Enter when ProtonVPN is open and ready… ")

    try:
        w_x, w_y, w_w, r2_top = ensure_searched_and_expanded()
    except Exception as exc:
        print(f"\nERROR preparing ProtonVPN: {exc}")
        print("Make sure ProtonVPN is open, signed in, and Accessibility permission "
              "is granted to your terminal/Python.")
        return 1

    print(f"\nProtonVPN window: pos=({w_x},{w_y}) width={w_w}; US header row y={r2_top}")
    print("The US list should now be EXPANDED. If it is NOT, click the expand arrow "
          "on the 'United States' row yourself before continuing.\n")
    print("Now I'll capture two hover positions. Hover the GREEN 'Connect' button that "
          "appears on the right of a server row when you point at it.\n")

    try:
        x1, y1 = capture_when_still("Connect button of the FIRST (top) US server")
        x2, y2 = capture_when_still("Connect button of the SECOND US server")
    except TimeoutError as exc:
        print(f"\nERROR: {exc}")
        return 1

    row_height = y2 - y1
    if row_height <= 0:
        print(f"\nERROR: second server (y={y2}) is not below the first (y={y1}). "
              "Please re-run and hover server 1 then server 2 top-to-bottom.")
        return 1
    connect_offset_from_right = (w_x + w_w) - x1
    header_height = y1 - r2_top - row_height // 2

    if abs(x1 - x2) > 15:
        print(f"\nWARNING: the two Connect buttons have different x ({x1} vs {x2}). "
              "They should be vertically aligned — using the first.")
    if not (10 <= row_height <= 200):
        print(f"\nWARNING: measured row_height={row_height}px looks unusual.")
    if not (0 <= connect_offset_from_right <= w_w):
        print(f"\nWARNING: connect_offset_from_right={connect_offset_from_right} is outside the window width.")

    values = {
        "connect_offset_from_right": connect_offset_from_right,
        "header_height": int(max(header_height, 1)),
        "row_height": int(row_height),
    }
    print("\n" + "=" * 70)
    print("Measured calibration for THIS Mac:")
    print(json.dumps(values, indent=2))
    print("=" * 70)

    write_calibration(values)
    print("\nDone. Now run run.command — it will use these values to connect.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
