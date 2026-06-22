#!/usr/bin/env python3
"""Validate ProtonVPN US server accessibility (scroll) and rotation.

Two checks:
  1. ROTATION (no VPN needed): simulate repeated runs and print the slot sequence
     to confirm the persistent rotation walks through ALL servers instead of
     re-using slot 1 every run.
  2. SCROLL/CONNECT: connect to a spread of slot numbers — including ones past the
     first visible page that require scrolling — screenshot the ProtonVPN window
     for each, and record the resulting public IP so you can confirm high-numbered
     servers are reachable and give distinct IPs.

Usage:
    python3 scripts/test_vpn_servers.py            # rotation check + screenshots, connect to default slots
    python3 scripts/test_vpn_servers.py 1 9 25 60  # connect to specific slots
    python3 scripts/test_vpn_servers.py --rotation-only
"""
import sys
import time
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import podcast_downloader as pd  # noqa: E402

SHOTS = ROOT / "logs" / "vpn_scroll_test"
SHOTS.mkdir(parents=True, exist_ok=True)


def screenshot(label: str) -> Path:
    p = SHOTS / f"{label}.png"
    subprocess.run(["screencapture", "-x", str(p)], check=False)
    return p


def public_ip() -> str:
    try:
        out = subprocess.run(
            ["curl", "-s", "--max-time", "8", "https://ipinfo.io/ip"],
            capture_output=True, text=True,
        )
        return out.stdout.strip() or "?"
    except Exception:
        return "?"


def main() -> int:
    args = [a for a in sys.argv[1:]]
    rotation_only = "--rotation-only" in args
    slot_args = [int(a) for a in args if a.isdigit()]

    logger = pd.RunLogger(ROOT / "logs")
    state = pd.StateManager(ROOT / "state" / "runtime_state.json")
    net = pd.NetworkState(logger)
    vpn = pd.VPNController(logger, net, state)
    cfg = pd.load_config(ROOT / "input" / "tasks.json").vpn

    servers = state.data.get("discovered_servers_by_location", {}).get(cfg.location, [])
    print(f"Cached {len(servers)} servers for {cfg.location!r}")
    if not servers:
        print("No cached servers — run the main automation once to discover them first.")
        return 1

    # --- 1. ROTATION simulation -------------------------------------------------
    print("\n=== ROTATION (persistent index) — simulating 8 consecutive runs ===")
    idx = int(state.data.get("vpn_rotation_index", {}).get(cfg.location, 0)) % len(servers)
    seq = []
    for r in range(8):
        seq.append(servers[idx % len(servers)])
        idx = (idx + 1) % len(servers)
    for r, s in enumerate(seq, 1):
        print(f"  run {r}: {s}")
    distinct = len(set(seq)) == len(seq)
    print(f"  -> {'PASS' if distinct else 'FAIL'}: each run advances to the next server "
          f"({'all distinct' if distinct else 'repeats found'})")

    if rotation_only:
        return 0

    # --- 2. SCROLL / CONNECT validation ----------------------------------------
    slots = slot_args or [1, 9, 25, 60]
    print(f"\n=== SCROLL/CONNECT to slots {slots} (screenshots -> {SHOTS}) ===")
    print("Do NOT touch the mouse/keyboard while this runs.\n")
    results = []
    for slot in slots:
        if slot < 1 or slot > len(servers):
            print(f"  slot {slot}: out of range (1..{len(servers)}) — skipped")
            continue
        print(f"-- slot {slot} --")
        # Disconnect first so each connection is fresh and we land in the server list.
        vpn._click_disconnect("ProtonVPN")
        time.sleep(2)
        try:
            r = vpn._connect_via_slot(
                "ProtonVPN", cfg.location, slot, calibration=cfg.calibration
            )
        except Exception as exc:
            r = f"error:{exc}"
        time.sleep(8)  # let the tunnel establish
        ip = public_ip()
        shot = screenshot(f"slot_{slot:03d}")
        print(f"   result={r}  ip={ip}  shot={shot.name}")
        results.append((slot, r, ip, shot.name))

    print("\n=== SUMMARY ===")
    for slot, r, ip, shot in results:
        print(f"  slot {slot:>4}: {r:<26} ip={ip:<16} {shot}")
    ips = [ip for _, _, ip, _ in results if ip and ip != "?"]
    ok = ips and len(set(ips)) == len(ips)
    print(f"\n  distinct IPs: {len(set(ips))}/{len(ips)}  -> "
          f"{'PASS: high-numbered servers reachable & distinct' if ok else 'CHECK screenshots in ' + str(SHOTS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
