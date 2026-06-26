#!/usr/bin/env python3
"""Apple Podcasts automation — state-driven, minimal-input.

Single-file orchestrator. Runs real macOS UI automation:
    python3 scripts/podcast_downloader.py

Input lives in input/tasks.json (only 4 keys). Working memory lives in
state/runtime_state.json. Both state/ and logs/ are auto-created.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import PyXA  # type: ignore
    HAS_PYXA = True
except ImportError:
    PyXA = None  # type: ignore
    HAS_PYXA = False


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
DEFAULT_VERIFY_TIMEOUT_SEC = 30
DEFAULT_SEE_ALL_BUDGET_SEC = 60
DEFAULT_ACCESSIBILITY_DEPTH = 20
DEFAULT_OSASCRIPT_TIMEOUT = 30
# Gap between consecutive episode download clicks — firing them back-to-back can
# make Podcasts drop/queue-fail the next download.
DOWNLOAD_GAP_SEC = 5.5
APPLE_PODCASTS_HOST = "podcasts.apple.com"

_COUNTRY_CODE_FALLBACK = {
    "united states": "US", "usa": "US", "america": "US",
    "united kingdom": "GB", "uk": "GB", "britain": "GB",
    "canada": "CA", "germany": "DE", "france": "FR", "spain": "ES",
    "italy": "IT", "netherlands": "NL", "switzerland": "CH",
    "japan": "JP", "australia": "AU", "singapore": "SG",
    "sweden": "SE", "norway": "NO", "ireland": "IE", "india": "IN",
}


class AutomationError(RuntimeError):
    pass


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class TabTask:
    tab: int
    videos: list[int]


@dataclass(frozen=True)
class VPNCalibration:
    """Per-device pixel geometry for the ProtonVPN server list.

    These are the only machine-dependent numbers in the connect routine.  They
    default to the values that work on the reference Mac, but differ across
    machines/displays/ProtonVPN versions, so they can be overridden per device
    via `vpn.calibration` in input/tasks.json (produced by scripts/calibrate.py).

    All offsets are anchored to values the connect routine reads live at runtime
    (the window's right edge and the US country-header row position `r2_top`), so
    they stay correct even when the window is moved between runs.
    """
    connect_offset_from_right: int = 38   # window_right_edge - Connect_button_x
    header_height: int = 48               # US country-header row height
    row_height: int = 48                  # individual server row height


@dataclass(frozen=True)
class VPNConfig:
    enabled: bool
    app: str = "Proton VPN"
    location: str = "United States"
    location_code: str = "US"
    servers: tuple[str, ...] = ()
    require_provider_in_org: bool = True
    verify_timeout: int = DEFAULT_VERIFY_TIMEOUT_SEC
    calibration: VPNCalibration = field(default_factory=VPNCalibration)


@dataclass(frozen=True)
class Config:
    repeat: int
    vpn: VPNConfig
    cleanup: bool
    tabs: list[TabTask]
    check_downloads: bool = False
    clean_start: bool = False
    cleanup_mode: str = "remove_download"  # "remove_download" | "remove_from_library"


def _parse_vpn(raw_vpn: Any) -> VPNConfig:
    if raw_vpn is None or raw_vpn is False:
        return VPNConfig(enabled=False)
    if raw_vpn is True:
        return VPNConfig(enabled=True)
    if not isinstance(raw_vpn, dict):
        raise ValueError("'vpn' must be a boolean or an object {enabled, app, location}")

    enabled = bool(raw_vpn.get("enabled", True))
    app = str(raw_vpn.get("app", "Proton VPN")).strip() or "Proton VPN"
    location = str(raw_vpn.get("location", "United States")).strip() or "United States"
    location_code = _COUNTRY_CODE_FALLBACK.get(location.lower(), location.upper()[:2] or "US")

    servers_raw = raw_vpn.get("servers", [])
    if not isinstance(servers_raw, list):
        raise ValueError("'vpn.servers' must be a list of strings (optional explicit override)")
    servers = tuple(str(s).strip() for s in servers_raw if str(s).strip())

    require_default = "proton" in app.lower()
    require = bool(raw_vpn.get("require_provider_in_org", require_default))

    cal_raw = raw_vpn.get("calibration", {})
    if not isinstance(cal_raw, dict):
        raise ValueError("'vpn.calibration' must be an object (see scripts/calibrate.py)")
    defaults = VPNCalibration()
    calibration = VPNCalibration(
        connect_offset_from_right=int(cal_raw.get("connect_offset_from_right",
                                                  defaults.connect_offset_from_right)),
        header_height=int(cal_raw.get("header_height", defaults.header_height)),
        row_height=int(cal_raw.get("row_height", defaults.row_height)),
    )

    return VPNConfig(
        enabled=enabled,
        app=app,
        location=location,
        location_code=location_code,
        servers=servers,
        require_provider_in_org=require,
        verify_timeout=int(raw_vpn.get("verify_timeout", DEFAULT_VERIFY_TIMEOUT_SEC)),
        calibration=calibration,
    )


def load_config(path: Path) -> Config:
    text = path.read_text(encoding="utf-8-sig")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        # Point at the offending line/column with the actual text, instead of dumping
        # a raw traceback — the usual cause is a stray/missing comma in tasks.json.
        lines = text.splitlines()
        snippet = lines[exc.lineno - 1] if 0 < exc.lineno <= len(lines) else ""
        pointer = " " * (max(exc.colno - 1, 0)) + "^"
        raise AutomationError(
            f"Invalid JSON in {path} at line {exc.lineno}, column {exc.colno}: "
            f"{exc.msg}.\n  {snippet}\n  {pointer}\n"
            f"Check tasks.json for a missing or extra comma, quote, or bracket."
        ) from None

    repeat = int(raw.get("repeat", 1))
    if repeat < 1:
        raise ValueError("repeat must be >= 1")

    cleanup = bool(raw.get("cleanup", False))
    check_downloads = bool(raw.get("check_downloads", cleanup))
    vpn = _parse_vpn(raw.get("vpn", False))

    tabs_raw = raw.get("tabs")
    if not isinstance(tabs_raw, list) or not tabs_raw:
        raise ValueError("'tabs' must be a non-empty list")

    tabs: list[TabTask] = []
    for i, item in enumerate(tabs_raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"tabs[{i}] must be an object")
        tab = item.get("tab")
        if not isinstance(tab, int) or tab < 1:
            raise ValueError(f"tabs[{i}].tab must be an integer >= 1")
        videos = item.get("videos")
        if not isinstance(videos, list) or not videos:
            raise ValueError(f"tabs[{i}].videos must be a non-empty list")
        if any(not isinstance(v, int) or v < 1 for v in videos):
            raise ValueError(f"tabs[{i}].videos must contain integers >= 1")
        tabs.append(TabTask(tab=tab, videos=sorted(set(videos))))

    clean_start = bool(raw.get("clean_start", False))

    cleanup_mode_raw = str(raw.get("cleanup_mode", "remove_download")).strip()
    if cleanup_mode_raw not in ("remove_download", "remove_from_library"):
        raise ValueError(
            f"cleanup_mode must be 'remove_download' or 'remove_from_library', got {cleanup_mode_raw!r}"
        )

    return Config(
        repeat=repeat,
        vpn=vpn,
        cleanup=cleanup,
        tabs=tabs,
        check_downloads=check_downloads,
        clean_start=clean_start,
        cleanup_mode=cleanup_mode_raw,
    )


# -----------------------------------------------------------------------------
# State Manager
# -----------------------------------------------------------------------------
class StateManager:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict[str, Any] = self._load_or_init()

    def _load_or_init(self) -> dict[str, Any]:
        defaults = self._default_state()
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    for key, value in defaults.items():
                        loaded.setdefault(key, value)
                    return loaded
            except json.JSONDecodeError:
                pass
        return defaults

    def _default_state(self) -> dict[str, Any]:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        return {
            "current_cycle": 0,
            "completed_cycles": [],
            "current_tab": None,
            "current_video": None,
            "used_public_ips": [],
            "used_vpn_servers": [],
            "discovered_servers_by_location": {},
            "last_public_ip": None,
            "last_vpn_server": None,
            "chrome_tabs_cache": {},
            "podcast_task_results": [],
            "download_check_results": [],
            "cleanup_results": [],
            "see_all_state": {},
            "cycle_phases": {},
            "last_failed_step": None,
            "last_error": None,
            "resume_available": True,
            "started_at": now,
            "updated_at": now,
            # v2 fields
            "processed_shows": {},          # {str(cycle): [{tab, url, show_name, videos_requested, videos_downloaded}]}
            "vpn_verify_level": None,       # "tunnel+route" | "tunnel+route+ip" | "tunnel+route+ip+country" | "tunnel_only"
            "download_state": None,         # "completed" | "in_progress" | "stable_unknown" | "timeout"
            "download_wait_seconds": None,
            "cleanup_fallback_keyboard_used": False,
        }

    def save(self) -> None:
        self.data["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def update(self, **fields: Any) -> None:
        self.data.update(fields)
        self.save()

    def record_failure(self, step: str, error: str, **context: Any) -> None:
        self.data["last_failed_step"] = step
        self.data["last_error"] = error
        for k, v in context.items():
            self.data[k] = v
        self.save()

    def add_task_result(self, **fields: Any) -> None:
        self.data["podcast_task_results"].append(fields)
        self.save()

    def add_cleanup_result(self, **fields: Any) -> None:
        self.data["cleanup_results"].append(fields)
        self.save()

    def add_download_check_result(self, **fields: Any) -> None:
        self.data["download_check_results"].append(fields)
        self.save()

    def mark_phase(self, cycle: int, phase: str) -> None:
        phases = self.data.setdefault("cycle_phases", {})
        phases.setdefault(str(cycle), {})[phase] = (
            datetime.now().astimezone().isoformat(timespec="seconds")
        )
        self.save()


# -----------------------------------------------------------------------------
# Logger
# -----------------------------------------------------------------------------
class RunLogger:
    def __init__(self, output_dir: Path):
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.log_path = output_dir / f"podcast-download-{stamp}.log"
        self.report_path = output_dir / f"podcast-download-{stamp}.json"
        self.events: list[dict[str, Any]] = []

    def log(self, message: str, step: str | None = None, **fields: Any) -> None:
        ts = datetime.now().astimezone().isoformat(timespec="seconds")
        prefix = f"STEP {step} | " if step else ""
        line = f"{ts} | {prefix}{message}"
        print(line, flush=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        event = {"timestamp": ts, "message": message}
        if step:
            event["step"] = step
        event.update(fields)
        self.events.append(event)

    def save_report(self, state: dict[str, Any]) -> None:
        payload = {
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "final_state": state,
            "events": self.events,
        }
        self.report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Report saved: {self.report_path}", flush=True)


# -----------------------------------------------------------------------------
# osascript wrapper
# -----------------------------------------------------------------------------
def run_osascript(
    script: str,
    timeout: int = DEFAULT_OSASCRIPT_TIMEOUT,
    label: str = "",
) -> str:
    if platform.system() != "Darwin":
        raise AutomationError("This script must run on macOS")
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise AutomationError(f"osascript timeout ({label}) after {timeout}s") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        if "-25211" in stderr or "assistive access" in stderr.lower():
            raise AutomationError(
                "Accessibility permission required.\n"
                "  Fix: System Settings → Privacy & Security → Accessibility\n"
                "       Add and enable Terminal (or the app you launched this from), then re-run.\n"
                f"  (raw: {stderr})"
            )
        raise AutomationError(f"osascript failed ({label}): {stderr}")
    return proc.stdout.strip()


# -----------------------------------------------------------------------------
# AppleScript helpers (bounded, no recursion)
# -----------------------------------------------------------------------------
# Each script template uses placeholders like __TARGET__ that we substitute with
# .replace(). f-strings are avoided so AppleScript braces stay readable.

_BOUNDED_HELPERS = r"""
on findButtonByName(rootElem, btnName, maxDepth)
    tell application "System Events"
        set stack to {{rootElem, 0}}
        repeat while (count of stack) > 0
            set lastPair to item -1 of stack
            if (count of stack) > 1 then
                set stack to items 1 thru -2 of stack
            else
                set stack to {}
            end if
            set elem to item 1 of lastPair
            set d to item 2 of lastPair
            try
                if exists button btnName of elem then return button btnName of elem
            end try
            if d < maxDepth then
                try
                    repeat with child in UI elements of elem
                        set end of stack to {child, d + 1}
                    end repeat
                end try
            end if
        end repeat
    end tell
    return missing value
end findButtonByName

on findButtonByDesc(rootElem, descKeyword, maxDepth)
    tell application "System Events"
        set stack to {{rootElem, 0}}
        repeat while (count of stack) > 0
            set lastPair to item -1 of stack
            if (count of stack) > 1 then
                set stack to items 1 thru -2 of stack
            else
                set stack to {}
            end if
            set elem to item 1 of lastPair
            set d to item 2 of lastPair
            try
                repeat with b in buttons of elem
                    set dd to ""
                    try
                        set dd to description of b
                    end try
                    set nn to ""
                    try
                        set nn to name of b
                    end try
                    if (dd contains descKeyword) or (nn contains descKeyword) then return b
                end repeat
            end try
            if d < maxDepth then
                try
                    repeat with child in UI elements of elem
                        set end of stack to {child, d + 1}
                    end repeat
                end try
            end if
        end repeat
    end tell
    return missing value
end findButtonByDesc

on textOfElement(e)
    tell application "System Events"
        try
            return value of static texts of e as text
        end try
        return ""
    end tell
end textOfElement
"""


# -----------------------------------------------------------------------------
# Network State
# -----------------------------------------------------------------------------
class NetworkState:
    def __init__(self, logger: RunLogger):
        self.logger = logger
        self._last_429_at: float = 0.0        # ipinfo.io
        self._last_429_at_ipapi: float = 0.0  # ip-api.com

    def snapshot(self) -> dict[str, Any]:
        info = self.public_ip_info()
        tunnels = self.active_tunnel_interfaces()
        return {
            "public_ip": (info or {}).get("ip"),
            "country": (info or {}).get("country"),
            "org": (info or {}).get("org"),
            "tunnel_interfaces": tunnels,
            "has_tunnel_interface": bool(tunnels),
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def public_ip_info(self) -> dict[str, Any] | None:
        """Return {ip, country (2-letter code), org} or None.

        Tries ip-api.com first (1000 req/min free tier), falls back to ipinfo.io.
        Each service tracks its own 429 backoff independently.
        """
        now = time.monotonic()

        # ── ip-api.com (primary — much higher rate limit) ────────────────────────
        if now - self._last_429_at_ipapi >= 60.0:
            try:
                req = urllib.request.Request(
                    "http://ip-api.com/json?fields=status,query,countryCode,org",
                    headers={"User-Agent": "podcast-downloader/2.0"},
                )
                with urllib.request.urlopen(req, timeout=6) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if data.get("status") == "success":
                    return {
                        "ip": data.get("query", ""),
                        "country": data.get("countryCode", ""),
                        "org": data.get("org", ""),
                    }
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    self._last_429_at_ipapi = time.monotonic()
                    self.logger.log("ip-api.com rate-limited (429) — backing off 60s",
                                    status="ipapi_429")
            except (urllib.error.URLError, TimeoutError, ValueError, OSError):
                pass  # fall through to ipinfo.io

        # ── ipinfo.io (fallback) ──────────────────────────────────────────────────
        import ssl
        ctx = ssl.create_default_context()

        if now - self._last_429_at < 60.0:
            return None  # both services rate-limited; caller uses tunnel fallback

        try:
            req = urllib.request.Request(
                "https://ipinfo.io/json",
                headers={"User-Agent": "podcast-downloader/2.0"},
            )
            with urllib.request.urlopen(req, timeout=6, context=ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                self._last_429_at = time.monotonic()
                self.logger.log("ipinfo.io rate-limited (429) — backing off 60s",
                                status="ip_429")
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            self.logger.log(f"Public IP lookup failed: {exc}", status="ip_check_failed")
        return None

    def active_tunnel_interfaces(self) -> list[str]:
        if platform.system() != "Darwin":
            return []
        try:
            out = subprocess.run(
                ["ifconfig"], capture_output=True, text=True, timeout=5
            ).stdout
        except (subprocess.SubprocessError, OSError):
            return []
        interfaces: list[str] = []
        current: str | None = None
        for line in out.splitlines():
            if line and not line.startswith("\t") and ":" in line:
                current = line.split(":", 1)[0]
            elif current and current.startswith("utun") and "inet " in line:
                if current not in interfaces:
                    interfaces.append(current)
        return interfaces

    def default_route_gateway(self) -> str:
        """Return the current default-route gateway IP, or '' on failure."""
        try:
            out = subprocess.run(
                ["route", "get", "default"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            for line in out.splitlines():
                stripped = line.strip()
                if stripped.startswith("gateway:"):
                    return stripped.split(":", 1)[1].strip()
        except (subprocess.SubprocessError, OSError):
            pass
        return ""

    def scutil_primary_interface(self) -> str:
        """Return the primary network interface name from scutil --nwi, or ''."""
        try:
            out = subprocess.run(
                ["scutil", "--nwi"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            for line in out.splitlines():
                stripped = line.strip()
                # Lines like: "   utun3 flags : ..."
                if stripped and not stripped.startswith("Network") and not stripped.startswith("DNS"):
                    iface = stripped.split()[0]
                    if iface:
                        return iface
        except (subprocess.SubprocessError, OSError):
            pass
        return ""


# -----------------------------------------------------------------------------
# VPN Controller
# -----------------------------------------------------------------------------
class VPNController:
    def __init__(self, logger: RunLogger, net: NetworkState, state: StateManager):
        self.logger = logger
        self.net = net
        self.state = state

    @staticmethod
    def _is_connected_to(
        ip_info: dict[str, Any] | None,
        target_cc: str,
        require_provider_in_org: bool,
        provider_name: str = "proton",
    ) -> bool:
        if not ip_info:
            return False
        country_ok = (ip_info.get("country") or "").upper() == target_cc.upper()
        if not require_provider_in_org:
            return country_ok
        org_ok = provider_name.lower() in (ip_info.get("org") or "").lower()
        return country_ok and org_ok

    def connect_with_config(self, cycle: int, vpn_cfg: VPNConfig) -> str:
        """Top-level entry point. Handles cache lookup, discovery, rotation, and verification."""
        provider_token = self._provider_token(vpn_cfg.app)

        # Pick the server list: explicit override > cached discovery > fresh discovery.
        servers = list(vpn_cfg.servers)
        if not servers:
            discovered = self.state.data.setdefault("discovered_servers_by_location", {})
            cached = discovered.get(vpn_cfg.location, [])
            # Use the cache only if it has >1 server.  A single-server cache means
            # the previous discovery run was incomplete (ProtonVPN's US list has many
            # servers); with only 1 server the rotation index never advances and the
            # same IP is used on every cycle.  Treat it as stale and re-discover.
            if cached and len(cached) > 1:
                servers = list(cached)
                self.logger.log(
                    f"Using cached server list for {vpn_cfg.location} ({len(servers)} servers)",
                    step="06", location=vpn_cfg.location, source="cache",
                )
            else:
                if cached:
                    self.logger.log(
                        f"Cached server list for {vpn_cfg.location} has only {len(cached)} "
                        f"server — treating as stale, re-discovering",
                        step="06", location=vpn_cfg.location,
                    )
                else:
                    self.logger.log(
                        f"No cached servers for {vpn_cfg.location}; running discovery in {vpn_cfg.app}",
                        step="06", location=vpn_cfg.location, app=vpn_cfg.app,
                    )
                if not self._open_provider_app(vpn_cfg.app):
                    raise AutomationError(
                        f"{vpn_cfg.app} app not found. Install and sign in first."
                    )
                servers = self._discover_servers(
                    vpn_cfg.location, vpn_cfg.location_code, vpn_cfg.app
                )
                if not servers:
                    raise AutomationError(
                        f"No servers discovered for {vpn_cfg.location} in {vpn_cfg.app}. "
                        f"Open {vpn_cfg.app}, search '{vpn_cfg.location}', expand the country, "
                        f"and try again — OR set vpn.servers explicitly in input/tasks.json."
                    )
                discovered[vpn_cfg.location] = servers
                self.state.save()
                self.logger.log(
                    f"Discovered {len(servers)} servers for {vpn_cfg.location}: {servers[:5]}"
                    + ("..." if len(servers) > 5 else ""),
                    step="06", location=vpn_cfg.location, server_count=len(servers),
                )

        # Ensure app is open.
        if not self._open_provider_app(vpn_cfg.app):
            raise AutomationError(f"{vpn_cfg.app} app not found.")

        # Baseline network state — capture route before any disconnect/connect.
        baseline_route = self.net.default_route_gateway()
        baseline = self.net.snapshot()
        baseline_ip = baseline.get("public_ip")
        self.state.data["vpn_baseline"] = baseline
        self.state.data["vpn_baseline_route"] = baseline_route
        self.state.save()
        self.logger.log(
            f"Baseline network: ip={baseline_ip} route={baseline_route} "
            f"country={baseline.get('country')} tunnels={baseline.get('tunnel_interfaces')}",
            step="06", baseline=baseline,
        )

        # Disconnect any current tunnel so we connect to the requested server fresh.
        disc = self._click_disconnect(vpn_cfg.app)
        self.logger.log(f"Pre-connect disconnect: {disc}", step="06", status=disc)
        if disc == "disconnect_clicked":
            time.sleep(1.5)
        ui_state = self._read_ui_connection_state(vpn_cfg.app)
        self.logger.log(f"{vpn_cfg.app} UI connection state: {ui_state}", step="06",
                        ui_connection_state=ui_state)

        # Re-capture baseline AFTER disconnect so baseline_ip reflects the bare (non-VPN) IP.
        # This handles the case where ProtonVPN auto-reconnects during the subsequent setup
        # AppleScript (which can take several seconds): the post-connect IP equals the
        # pre-disconnect VPN IP, making ip==baseline_ip (even though connection succeeded).
        # If the IP hasn't changed yet (auto-reconnect or slow teardown), disable the
        # ip-change guard by using None — country + tunnel-interface checks are sufficient.
        post_disc_snap = self.net.snapshot()
        post_disc_ip = post_disc_snap.get("public_ip")
        if post_disc_ip and post_disc_ip != baseline_ip:
            baseline_ip = post_disc_ip
            self.logger.log(f"Post-disconnect baseline: ip={baseline_ip}", step="06")
        elif baseline.get("has_tunnel_interface"):
            baseline_ip = None
            self.logger.log(
                "Post-disconnect IP unchanged from VPN baseline — ip-change check disabled",
                step="06",
            )

        # Build a try-order: start at the next server in a PERSISTENT rotation and
        # wrap through all. The pointer is stored in state per location and advances
        # by one on every connection, so the repeated flow walks through ALL servers
        # instead of always starting at slot 1 on each fresh run. (The old
        # `(cycle - 1) % len` reset to slot 1 every run, which is why only one IP was
        # ever used.) Persist the advance immediately so a crash/next run continues
        # from the next server.
        rot = self.state.data.setdefault("vpn_rotation_index", {})
        start_idx = int(rot.get(vpn_cfg.location, 0)) % len(servers)
        rot[vpn_cfg.location] = (start_idx + 1) % len(servers)
        self.state.save()
        self.logger.log(
            f"VPN rotation: starting at slot index {start_idx} "
            f"({servers[start_idx]}); next run will use index {rot[vpn_cfg.location]}",
            step="06", rotation_index=start_idx,
        )
        servers_to_try = [servers[(start_idx + off) % len(servers)]
                          for off in range(len(servers))]
        last_exc: AutomationError | None = None
        slot_baseline_ip = baseline_ip  # refreshed per attempt
        slot_baseline_route = baseline_route  # refreshed per attempt

        for attempt_i, target_server in enumerate(servers_to_try):
            if attempt_i > 0:
                self.logger.log(
                    f"Slot {servers_to_try[attempt_i - 1]} failed; retrying with {target_server}",
                    step="06", server=target_server,
                )
                disc2 = self._click_disconnect(vpn_cfg.app)
                self.logger.log(f"Retry pre-disconnect: {disc2}", step="06", status=disc2)
                wait_s = 5.0 if disc2 == "disconnect_clicked" else 2.0
                time.sleep(wait_s)
                snap2 = self.net.snapshot()
                slot_baseline_ip = snap2.get("public_ip")
                slot_baseline_route = self.net.default_route_gateway()
                self.logger.log(
                    f"Retry baseline: ip={slot_baseline_ip} route={slot_baseline_route}", step="06",
                )

            self.logger.log(
                f"Cycle {cycle} target {vpn_cfg.app} server: {target_server} "
                f"(attempt {attempt_i + 1}/{len(servers_to_try)})",
                step="06", cycle=cycle, target_server=target_server,
            )

            ui_status = self._click_server_by_name(
                vpn_cfg.app, target_server, vpn_cfg.location,
                force_retype=(attempt_i > 0), calibration=vpn_cfg.calibration,
            )
            self.logger.log(
                f"{vpn_cfg.app} server '{target_server}': {ui_status}",
                step="06", status=ui_status, server=target_server,
            )
            if ui_status not in (
                "server_clicked",
                "row_clicked",
                "connect_button_clicked",
                "quick_connect_clicked",
                "connect_clicked",
            ):
                last_exc = AutomationError(
                    f"Could not click server '{target_server}' in {vpn_cfg.app}: {ui_status}"
                )
                continue

            try:
                result = self._poll_verify(
                    baseline_ip=slot_baseline_ip,
                    target_cc=vpn_cfg.location_code,
                    provider_token=provider_token,
                    require_provider_in_org=vpn_cfg.require_provider_in_org,
                    verify_timeout=vpn_cfg.verify_timeout,
                    baseline_route=slot_baseline_route,
                )
                if result == "connected_verified":
                    self._record_server(target_server)
                    # Record full verified session (slot name, not assumed real server name)
                    snap = self.net.snapshot()
                    sessions = self.state.data.setdefault("vpn_sessions", [])
                    sessions.append({
                        "cycle": cycle,
                        "slot": target_server,
                        "verified": True,
                        "utun": (snap.get("tunnel_interfaces") or [None])[0],
                        "public_ip": snap.get("public_ip"),
                        "country": snap.get("country"),
                        "verified_at": datetime.now().isoformat(),
                    })
                    self.state.save()
                return result
            except AutomationError as exc:
                last_exc = exc
                continue

        raise last_exc or AutomationError("All VPN slots exhausted without a verified connection")

    @staticmethod
    def _provider_token(app_name: str) -> str:
        """Substring expected to appear in ipinfo.org for the provider."""
        n = app_name.lower()
        if "proton" in n:
            return "proton"
        if "mullvad" in n:
            return "mullvad"
        if "nordvpn" in n or "nord vpn" in n:
            return "nordvpn"
        if "expressvpn" in n or "express vpn" in n:
            return "expressvpn"
        return n.split()[0]

    def _open_provider_app(self, app_name: str) -> str:
        candidates = [app_name, app_name.replace(" ", "")]
        for name in candidates:
            proc = subprocess.run(["open", "-a", name], capture_output=True, text=True)
            if proc.returncode == 0:
                return name
        return ""

    def _record_server(self, server: str) -> None:
        self.state.data["last_vpn_server"] = server
        used = list(self.state.data.get("used_vpn_servers", []))
        used.append(server)
        self.state.data["used_vpn_servers"] = used
        self.state.save()

    def _process_name_candidates(self, app_name: str) -> str:
        """AppleScript list literal of process name candidates for this app."""
        names = [app_name, app_name.replace(" ", "")]
        # dedupe while preserving order
        seen: list[str] = []
        for n in names:
            if n not in seen:
                seen.append(n)
        return ", ".join('"' + n.replace('"', '\\"') + '"' for n in seen)

    def _discover_servers(
        self, location: str, location_code: str, app_name: str = "ProtonVPN"
    ) -> list[str]:
        """Discover the real number of available servers by expanding the country list.

        Runs the same Phase 1-3 ProtonVPN UI setup as _connect_via_slot to obtain
        window coordinates, then expands the country row and counts server rows via:
          1. AX table row count (works on the *filtered* list, which has ~50-100 rows,
             not the unfiltered 6000+ row table).
          2. Pixel brightness sampling fallback if AX times out.

        Returns positional slot tokens (e.g. US-SLOT-1 … US-SLOT-N) because ProtonVPN
        lazy-renders label text — real server names are only readable after hover.
        Leaves ProtonVPN in the expanded/filtered state so the immediately following
        _connect_via_slot(slot=1) call finds the list already expanded.
        """
        try:
            import Quartz  # type: ignore[import]
        except ImportError:
            self.logger.log(
                f"Quartz unavailable for discovery — using 5 positional slots",
                step="06", location=location,
            )
            return [f"{location_code.upper()}-SLOT-{i + 1}" for i in range(5)]

        process_list = self._process_name_candidates(app_name)

        def _dmouse(kind, x, y):
            pt = Quartz.CGPoint(x=float(x), y=float(y))
            ev = Quartz.CGEventCreateMouseEvent(None, kind, pt, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.05)

        def _dkey(vk, down, flags=0):
            src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateCombinedSessionState)
            ev = Quartz.CGEventCreateKeyboardEvent(src, vk, down)
            if flags:
                Quartz.CGEventSetFlags(ev, flags)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.05)

        def _fallback(n: int = 5) -> list[str]:
            return [f"{location_code.upper()}-SLOT-{i + 1}" for i in range(n)]

        # ── Phase 1: get search-field + window coordinates ────────────────────
        p1_script = f"""
        tell application "System Events"
            set procName to ""
            repeat with candidate in {{{process_list}}}
                if exists process (candidate as text) then
                    set procName to candidate as text
                    exit repeat
                end if
            end repeat
            if procName is "" then return "ERROR:no_process"
            tell process procName
                set frontmost to true
                delay 0.5
                if not (exists window 1) then return "ERROR:no_window"
                if not (exists text field 1 of group 1 of window 1) then return "ERROR:no_sf"
                set sf to text field 1 of group 1 of window 1
                set sfPos to position of sf
                set sfSz to size of sf
                set wPos to position of window 1
                set wSz to size of window 1
                return "SF:" & (item 1 of sfPos) & "," & (item 2 of sfPos) & "," & ¬
                                (item 1 of sfSz) & "," & (item 2 of sfSz) & ¬
                       "|W:" & (item 1 of wPos) & "," & (item 2 of wPos) & "," & ¬
                               (item 1 of wSz)  & "," & (item 2 of wSz)
            end tell
        end tell
        """
        try:
            p1 = run_osascript(p1_script, timeout=30, label=f"discover-p1 {location}")
        except AutomationError as exc:
            self.logger.log(f"Discovery p1 failed ({exc}) — using 5 slots", step="06")
            return _fallback()

        if p1.startswith("ERROR:"):
            self.logger.log(f"Discovery p1 error: {p1} — using 5 slots", step="06")
            return _fallback()

        sf_x = sf_y = sf_w = sf_h = 0
        w_x = w_y = w_w = w_h = 0
        for chunk in p1.split("|"):
            if chunk.startswith("SF:"):
                parts = chunk[3:].split(",")
                if len(parts) == 4:
                    sf_x, sf_y, sf_w, sf_h = (int(p) for p in parts)
            elif chunk.startswith("W:"):
                parts = chunk[2:].split(",")
                if len(parts) == 4:
                    w_x, w_y, w_w, w_h = (int(p) for p in parts)

        if sf_w == 0 or w_w == 0:
            self.logger.log(f"Discovery p1 bad data: {p1!r} — using 5 slots", step="06")
            return _fallback()

        # ── Phase 2: paste search filter ──────────────────────────────────────
        sf_cx, sf_cy = sf_x + sf_w // 2, sf_y + sf_h // 2
        old_clip = subprocess.run(["pbpaste"], capture_output=True).stdout
        try:
            subprocess.run(["pbcopy"], input=location.encode(), check=True)
            _dmouse(Quartz.kCGEventLeftMouseDown, sf_cx, sf_cy)
            _dmouse(Quartz.kCGEventLeftMouseUp,   sf_cx, sf_cy)
            time.sleep(0.4)
            _dkey(0x00, True,  Quartz.kCGEventFlagMaskCommand)   # Cmd+A
            _dkey(0x00, False, Quartz.kCGEventFlagMaskCommand)
            time.sleep(0.1)
            _dkey(0x33, True); _dkey(0x33, False)                # Backspace
            time.sleep(0.5)
            _dkey(0x09, True,  Quartz.kCGEventFlagMaskCommand)   # Cmd+V
            _dkey(0x09, False, Quartz.kCGEventFlagMaskCommand)
            time.sleep(2.5)  # wait for ProtonVPN to filter + auto-expand the country row
            self.logger.log(f"Discovery: search filter pasted '{location}'", step="06")
        finally:
            subprocess.run(["pbcopy"], input=old_clip, check=False)

        # ── Phase 3: scroll to top, get first-state position ─────────────────
        p3_script = f"""
        tell application "System Events"
            set procName to ""
            repeat with candidate in {{{process_list}}}
                if exists process (candidate as text) then
                    set procName to candidate as text
                    exit repeat
                end if
            end repeat
            if procName is "" then return "ERROR:no_process"
            tell process procName
                if not (exists group 1 of window 1) then return "ERROR:no_group"
                if not (exists scroll area 1 of group 1 of window 1) then return "ERROR:no_scroll"
                set sc to scroll area 1 of group 1 of window 1
                try
                    set value of scroll bar 1 of sc to 0
                    delay 0.3
                end try
                if not (exists UI element 1 of sc) then return "ERROR:no_outer_list"
                set outerList to UI element 1 of sc
                set stateList to missing value
                set headerY to 0
                repeat with c in UI elements of outerList
                    set cdd to ""
                    try
                        set cdd to description of c as text
                    end try
                    if cdd is "list" then
                        set stateList to c
                    end if
                    if (class of c as text) is "button" and headerY = 0 then
                        try
                            set csz to size of c
                            if (item 1 of csz) > 100 then
                                set cpos to position of c
                                set headerY to (item 2 of cpos) as integer
                            end if
                        end try
                    end if
                end repeat
                set wPos to position of window 1
                set wSz to size of window 1
                if stateList is missing value then
                    return "R2:" & headerY & "|W:" & (item 1 of wPos) & "," & (item 2 of wPos) & "," & (item 1 of wSz) & "," & (item 2 of wSz) & "|EXP:0"
                end if
                if not (exists UI element 1 of stateList) then return "ERROR:empty_state_list"
                set firstElem to UI element 1 of stateList
                set fPos to position of firstElem
                return "R2:" & (item 2 of fPos) & "|W:" & (item 1 of wPos) & "," & (item 2 of wPos) & "," & (item 1 of wSz) & "," & (item 2 of wSz) & "|EXP:1"
            end tell
        end tell
        """
        try:
            p3 = run_osascript(p3_script, timeout=20, label=f"discover-p3 {location}")
        except AutomationError as exc:
            self.logger.log(f"Discovery p3 failed ({exc}) — using 5 slots", step="06")
            return _fallback()

        if p3.startswith("ERROR:"):
            self.logger.log(f"Discovery p3 error: {p3} — using 5 slots", step="06")
            return _fallback()

        r2_top = 0
        is_expanded_p3 = False
        for chunk in p3.split("|"):
            if chunk.startswith("R2:"):
                try:
                    r2_top = int(chunk[3:])
                except ValueError:
                    pass
            elif chunk.startswith("W:"):
                parts = chunk[2:].split(",")
                if len(parts) == 4:
                    w_x, w_y, w_w, w_h = (int(p) for p in parts)
            elif chunk.startswith("EXP:"):
                is_expanded_p3 = chunk[4:].strip() == "1"

        if r2_top == 0:
            self.logger.log(f"Discovery p3 bad data: {p3!r} — using 5 slots", step="06")
            return _fallback()

        # New Mac: row height = 44px; US header height ≈ row height.
        SERVER_ROW_H = 44
        expand_x = w_x + w_w // 2
        expand_y = r2_top + SERVER_ROW_H // 2

        # ── Phase 4: expand if needed, then count state rows ─────────────────
        subprocess.run(
            ["osascript", "-e",
             f'tell application "System Events" to set frontmost of process "ProtonVPN" to true'],
            timeout=4, check=False,
        )
        time.sleep(0.2)

        self.logger.log(f"Discovery: p3 expanded={is_expanded_p3}", step="06")
        if not is_expanded_p3:
            _dmouse(Quartz.kCGEventLeftMouseDown, expand_x, expand_y)
            _dmouse(Quartz.kCGEventLeftMouseUp,   expand_x, expand_y)
            time.sleep(1.5)

        # ── Count server rows ─────────────────────────────────────────────────
        # Primary: AX row count (fast on the *filtered* table, ~50-100 rows max).
        # After filtering to a single country the table is small; the 6000+ row
        # problem only appears on the unfiltered table.
        count_script = f"""
        tell application "System Events"
            set procName to ""
            repeat with candidate in {{{process_list}}}
                if exists process (candidate as text) then
                    set procName to candidate as text
                    exit repeat
                end if
            end repeat
            if procName is "" then return "ERROR:no_process"
            tell process procName
                if not (exists scroll area 1 of group 1 of window 1) then return "ERROR:no_scroll"
                set sc to scroll area 1 of group 1 of window 1
                if not (exists UI element 1 of sc) then return "ERROR:no_outer_list"
                set outerList to UI element 1 of sc
                set stateList to missing value
                repeat with c in UI elements of outerList
                    set cdd to ""
                    try
                        set cdd to description of c as text
                    end try
                    if cdd is "list" then
                        set stateList to c
                        exit repeat
                    end if
                end repeat
                if stateList is missing value then return "ERROR:no_state_list"
                -- Each state has 2 elements (name button + three-dot button)
                set elemCount to count of UI elements of stateList
                set stateCount to elemCount div 2
                if stateCount < 1 then set stateCount to 1
                return stateCount as text
            end tell
        end tell
        """
        server_count = 0
        try:
            cr = run_osascript(count_script, timeout=15, label=f"discover-count {location}")
            cr = cr.strip()
            if not cr.startswith("ERROR:"):
                server_count = int(cr)
                self.logger.log(
                    f"Discovery: AX element count → {server_count} states for {location}",
                    step="06",
                )
        except (AutomationError, ValueError) as exc:
            self.logger.log(
                f"Discovery: AX count failed ({exc}) — defaulting to 10 states",
                step="06",
            )

        if server_count <= 0:
            server_count = 10
            self.logger.log(
                f"Discovery: AX count returned 0 — using default {server_count} states",
                step="06",
            )

        server_count = max(server_count, 1)
        slots = [f"{location_code.upper()}-SLOT-{i + 1}" for i in range(server_count)]
        self.logger.log(
            f"Discovery complete: {len(slots)} positional slots for {location}",
            step="06", location=location, slot_count=len(slots),
        )
        return slots

    def _click_server_by_name(
        self, app_name: str, server: str, location: str = "", force_retype: bool = False,
        calibration: "VPNCalibration | None" = None,
    ) -> str:
        """Route to slot-based connect or (future) named-server connect."""
        if "-SLOT-" in server:
            try:
                slot_num = int(server.split("-SLOT-")[1])
            except (ValueError, IndexError):
                slot_num = 1
            return self._connect_via_slot(
                app_name, location or server.split("-SLOT-")[0], slot_num,
                force_retype=force_retype, calibration=calibration,
            )
        # Named server fallback (not normally reached with slot discovery).
        process_list = self._process_name_candidates(app_name)
        server_esc = server.replace('"', '\\"')
        script = f"""
        tell application "System Events"
            set procName to ""
            repeat with candidate in {{{process_list}}}
                if exists process (candidate as text) then
                    set procName to candidate as text
                    exit repeat
                end if
            end repeat
            if procName is "" then return "vpn_process_not_found"
            tell process procName
                set frontmost to true
                delay 0.5
                if not (exists window 1) then return "no_window"
                if not (exists text field 1 of group 1 of window 1) then return "search_field_not_found"
                set sf to text field 1 of group 1 of window 1
                try
                    set focused of sf to true
                    delay 0.1
                end try
                set value of sf to "{server_esc}"
                delay 0.2
                key code 36
                delay 1.5
                set tbl to table 1 of scroll area 1 of window 1
                if (count of rows of tbl) > 0 then
                    click row 1 of tbl
                    return "row_clicked"
                end if
                return "server_not_found"
            end tell
        end tell
        """
        return run_osascript(script, timeout=20, label=f"click server {server}")

    def _connect_via_slot(
        self, app_name: str, location: str, slot_num: int, force_retype: bool = False,
        calibration: "VPNCalibration | None" = None,
    ) -> str:
        """Search for location in ProtonVPN, expand the country row via Quartz click,
        then hover+click the Nth server row's Connect button.

        All accessibility reads happen BEFORE expansion (table has 2 rows, fast).
        After expansion the table has 6000+ rows and any accessibility op times out,
        so Quartz handles both the expansion click and the Connect hover+click.

        Row heights are empirically fixed in ProtonVPN 4.x:
          - "All locations" header: 32 px
          - Country header row:     48 px
          - Individual server rows: 48 px
        Connect button appears at right_edge - 38 px on hover (default; see
        VPNCalibration / scripts/calibrate.py for per-device overrides).
        """
        if calibration is None:
            calibration = VPNCalibration()
        process_list = self._process_name_candidates(app_name)

        # ProtonVPN Mac Catalyst text field accepts NEITHER AppleScript keystroke NOR
        # Quartz CGEventKeyboardSetUnicodeString — both are silently swallowed.
        # The ONLY reliable input path is clipboard paste (Cmd+V) after a Quartz click
        # focuses the field.  So we:
        #   Phase 1 – AppleScript: focus ProtonVPN, return sf pixel position + window pos.
        #   Phase 2 – Quartz:     click sf, Cmd+A+Del to clear, Cmd+V to paste location.
        #   Phase 3 – AppleScript: scroll-to-top, read row-2 position (filter already applied).
        #   Phase 4 – Quartz:     expansion click (if needed), hover, click Connect button.

        try:
            import Quartz  # type: ignore[import]
        except ImportError:
            self.logger.log("Quartz unavailable — cannot connect", step="06")
            return "quartz_unavailable"

        def _mouse(kind, x, y):
            pt = Quartz.CGPoint(x=float(x), y=float(y))
            ev = Quartz.CGEventCreateMouseEvent(None, kind, pt, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.05)

        def _warp(x, y):
            # Move the REAL hardware cursor, then post a mouse-moved event.
            # ProtonVPN's Mac Catalyst Connect button is rendered only while the
            # cursor hovers the row (an NSTrackingArea), and tracking follows the
            # *actual* cursor position — a synthetic kCGEventMouseMoved alone does
            # not reliably enter the tracking area on every Mac (it worked on one
            # mini, not another, where the button never appeared and the click hit
            # nothing).  CGWarpMouseCursorPosition guarantees the cursor is really
            # over the row so the button paints before we click it.
            pt = Quartz.CGPoint(x=float(x), y=float(y))
            Quartz.CGWarpMouseCursorPosition(pt)
            ev = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, pt,
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

        # ── Phase 1: get sf + window coordinates ─────────────────────────────────────
        phase1_script = f"""
        tell application "System Events"
            set procName to ""
            repeat with candidate in {{{process_list}}}
                if exists process (candidate as text) then
                    set procName to candidate as text
                    exit repeat
                end if
            end repeat
            if procName is "" then return "ERROR:vpn_process_not_found"
            tell process procName
                set frontmost to true
                delay 0.5
                if not (exists window 1) then return "ERROR:no_window"
                if not (exists text field 1 of group 1 of window 1) then return "ERROR:no_search_field"
                set sf to text field 1 of group 1 of window 1
                set sfPos to position of sf
                set sfSz to size of sf
                set sfX to (item 1 of sfPos) as integer
                set sfY to (item 2 of sfPos) as integer
                set sfW to (item 1 of sfSz) as integer
                set sfH to (item 2 of sfSz) as integer
                set wPos to position of window 1
                set wSz to size of window 1
                set wX to (item 1 of wPos) as integer
                set wY to (item 2 of wPos) as integer
                set wW to (item 1 of wSz) as integer
                return "SF:" & sfX & "," & sfY & "," & sfW & "," & sfH & "|W:" & wX & "," & wY & "," & wW
            end tell
        end tell
        """
        try:
            p1 = run_osascript(phase1_script, timeout=30, label=f"slot-connect phase1 {location}")
        except AutomationError as exc:
            self.logger.log(f"Slot connect phase1 failed: {exc}", step="06", status="slot_setup_failed")
            if "Accessibility permission required" in str(exc):
                raise
            return "slot_setup_failed"

        if p1.startswith("ERROR:"):
            self.logger.log(f"Slot connect phase1: {p1}", step="06", status=p1)
            return p1

        sf_x = sf_y = sf_w = sf_h = 0
        w_x = w_y = w_w = 0
        for chunk in p1.split("|"):
            if chunk.startswith("SF:"):
                nums = chunk[3:].split(",")
                if len(nums) == 4:
                    sf_x, sf_y, sf_w, sf_h = int(nums[0]), int(nums[1]), int(nums[2]), int(nums[3])
            elif chunk.startswith("W:"):
                nums = chunk[2:].split(",")
                if len(nums) == 3:
                    w_x, w_y, w_w = int(nums[0]), int(nums[1]), int(nums[2])

        if sf_w == 0 or w_w == 0:
            self.logger.log(f"Bad phase1 data: {p1!r}", step="06")
            return "bad_anchor_data"

        # ── Phase 2: Quartz clipboard-paste to filter the server list ─────────────────
        # AppleScript keystroke and Quartz Unicode-string injection are both silently
        # ignored by ProtonVPN's Mac Catalyst text field.  Cmd+V paste IS accepted and
        # also triggers the incremental search filter (rows collapse from ~181 → 2).
        sf_cx = sf_x + sf_w // 2
        sf_cy = sf_y + sf_h // 2

        # Save clipboard so we can restore it afterwards.
        old_clip = subprocess.run(["pbpaste"], capture_output=True).stdout

        try:
            subprocess.run(["pbcopy"], input=location.encode(), check=True)

            # Click search field to focus it.
            _mouse(Quartz.kCGEventLeftMouseDown, sf_cx, sf_cy)
            _mouse(Quartz.kCGEventLeftMouseUp, sf_cx, sf_cy)
            time.sleep(0.4)

            # Cmd+A to select all existing text, then Delete to clear.
            _key(0x00, True,  Quartz.kCGEventFlagMaskCommand)   # Cmd+A down
            _key(0x00, False, Quartz.kCGEventFlagMaskCommand)   # Cmd+A up
            time.sleep(0.1)
            _key(0x33, True)   # Backspace down
            _key(0x33, False)  # Backspace up
            time.sleep(0.5)

            # Cmd+V to paste the location string.
            _key(0x09, True,  Quartz.kCGEventFlagMaskCommand)   # Cmd+V down
            _key(0x09, False, Quartz.kCGEventFlagMaskCommand)   # Cmd+V up
            time.sleep(2.5)  # wait for ProtonVPN to filter + auto-expand the country row

            self.logger.log(f"Search filter pasted: '{location}'", step="06")
        finally:
            # Restore original clipboard.
            subprocess.run(["pbcopy"], input=old_clip, check=False)

        # ── Phase 3: scroll to top, read state-list position ────────────────────────────
        # New Mac AX structure: scroll area is inside group 1 (not a direct window child).
        # The list uses nested UI elements instead of a table with rows.
        phase3_script = f"""
        tell application "System Events"
            set procName to ""
            repeat with candidate in {{{process_list}}}
                if exists process (candidate as text) then
                    set procName to candidate as text
                    exit repeat
                end if
            end repeat
            if procName is "" then return "ERROR:vpn_process_not_found"
            tell process procName
                if not (exists group 1 of window 1) then return "ERROR:no_group"
                if not (exists scroll area 1 of group 1 of window 1) then return "ERROR:no_scroll_area"
                set sc to scroll area 1 of group 1 of window 1
                try
                    set value of scroll bar 1 of sc to 0
                    delay 0.3
                end try
                if not (exists UI element 1 of sc) then return "ERROR:no_outer_list"
                set outerList to UI element 1 of sc
                -- Walk outer list: find inner state list (dd="list") and US header y
                set stateList to missing value
                set headerY to 0
                repeat with c in UI elements of outerList
                    set cdd to ""
                    try
                        set cdd to description of c as text
                    end try
                    if cdd is "list" then
                        set stateList to c
                    end if
                    -- Use first WIDE button (w>100) as the US row expand button.
                    -- Narrow buttons (e.g. info icon w=16) are skipped.
                    if (class of c as text) is "button" and headerY = 0 then
                        try
                            set csz to size of c
                            if (item 1 of csz) > 100 then
                                set cpos to position of c
                                set headerY to (item 2 of cpos) as integer
                            end if
                        end try
                    end if
                end repeat
                set wPos to position of window 1
                set wSz to size of window 1
                set wX to (item 1 of wPos) as integer
                set wY to (item 2 of wPos) as integer
                set wW to (item 1 of wSz) as integer
                set wH to (item 2 of wSz) as integer
                if stateList is missing value then
                    -- US collapsed; return header y for expand click. SC:0 = unknown count.
                    return "R2:0," & headerY & "|W:" & wX & "," & wY & "," & wW & "," & wH & "|EXP:0|RH:44|SC:0"
                end if
                if not (exists UI element 1 of stateList) then return "ERROR:empty_state_list"
                set firstElem to UI element 1 of stateList
                set fPos to position of firstElem
                set fSz to size of firstElem
                set firstY to (item 2 of fPos) as integer
                set rowH to (item 2 of fSz) as integer
                -- Each state has 2 elements (name button + three-dot button)
                set elemCount to count of UI elements of stateList
                set stateCount to elemCount div 2
                if stateCount < 1 then set stateCount to 1
                return "R2:0," & firstY & "|W:" & wX & "," & wY & "," & wW & "," & wH & "|EXP:1|RH:" & rowH & "|SC:" & stateCount
            end tell
        end tell
        """
        try:
            p3 = run_osascript(phase3_script, timeout=20, label=f"slot-connect phase3 {location}")
        except AutomationError as exc:
            self.logger.log(f"Slot connect phase3 failed: {exc}", step="06", status="slot_setup_failed")
            if "Accessibility permission required" in str(exc):
                raise
            return "slot_setup_failed"

        if p3.startswith("ERROR:"):
            self.logger.log(f"Slot connect phase3: {p3}", step="06", status=p3)
            return p3

        r2_x = r2_top = 0
        w_x2 = w_y2 = w_w2 = w_h2 = 0
        already_expanded = False
        row_h_from_p3 = 0
        state_count_from_p3 = 0
        for chunk in p3.split("|"):
            if chunk.startswith("R2:"):
                nums = chunk[3:].split(",")
                if len(nums) == 2:
                    r2_x, r2_top = int(nums[0]), int(nums[1])
            elif chunk.startswith("W:"):
                nums = chunk[2:].split(",")
                if len(nums) >= 4:
                    w_x2, w_y2, w_w2, w_h2 = (
                        int(nums[0]), int(nums[1]), int(nums[2]), int(nums[3])
                    )
                elif len(nums) == 3:
                    w_x2, w_y2, w_w2 = int(nums[0]), int(nums[1]), int(nums[2])
            elif chunk.startswith("EXP:"):
                already_expanded = chunk[4:].strip() == "1"
            elif chunk.startswith("RH:"):
                try:
                    row_h_from_p3 = int(chunk[3:])
                except ValueError:
                    pass
            elif chunk.startswith("SC:"):
                try:
                    state_count_from_p3 = int(chunk[3:])
                except ValueError:
                    pass

        # Use phase3 window coords if available (most current), fall back to phase1.
        w_h = w_h2  # window height (phase1 does not capture it; 0 disables scroll)
        if w_w2 > 0:
            w_x, w_y, w_w = w_x2, w_y2, w_w2

        if r2_top == 0 or w_w == 0:
            self.logger.log(f"Bad phase3 data: {p3!r}", step="06")
            return "bad_anchor_data"

        # Row height comes from Phase 3 AX measurement; calibration is the fallback.
        SERVER_ROW_H = row_h_from_p3 if row_h_from_p3 > 0 else calibration.row_height

        # Wrap slot_num within the actual state count so rotation always lands on
        # a real state (state_count_from_p3=0 means not yet known — use slot as-is).
        total_slots = state_count_from_p3 or len(
            self.state.data.get("discovered_servers_by_location", {}).get(location, [])
        ) or slot_num
        effective_slot = ((slot_num - 1) % total_slots) + 1 if total_slots > 1 else slot_num

        connect_x = w_x + w_w - calibration.connect_offset_from_right

        if already_expanded:
            # r2_top is the first state's y; US header is one row_h above it.
            expand_y = r2_top - SERVER_ROW_H // 2
            server_y = r2_top + (effective_slot - 1) * SERVER_ROW_H + SERVER_ROW_H // 2
        else:
            # r2_top is the US header button's y; first state is one row_h below.
            expand_y = r2_top + SERVER_ROW_H // 2
            server_y = r2_top + SERVER_ROW_H + (effective_slot - 1) * SERVER_ROW_H + SERVER_ROW_H // 2

        expand_x = w_x + w_w // 2

        self.logger.log(
            f"Slot {slot_num} (eff {effective_slot}/{total_slots}): r2=({r2_x},{r2_top}) "
            f"w=({w_x},{w_y},{w_w}) row_h={SERVER_ROW_H} "
            f"already_expanded={already_expanded} expand=({expand_x},{expand_y}) "
            f"server_y={server_y}",
            step="06", slot=slot_num,
        )

        # ── Phase 4: ensure expanded → click three-dot button ───────────────────────────

        subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to set frontmost of process "ProtonVPN" to true'],
            timeout=4, check=False,
        )
        time.sleep(0.3)
        _mouse(Quartz.kCGEventMouseMoved, w_x + w_w // 2, expand_y - 30)
        time.sleep(0.2)

        # If the state list isn't visible yet, click the expand arrow on the US row.
        if already_expanded:
            self.logger.log("State list already expanded (AX) — skipping expand click", step="06")
        else:
            self.logger.log("State list collapsed (AX) — clicking expand arrow", step="06")
            _mouse(Quartz.kCGEventLeftMouseDown, expand_x, expand_y)
            _mouse(Quartz.kCGEventLeftMouseUp,   expand_x, expand_y)
            time.sleep(1.5)  # wait for expansion animation

        # Bring the target state into view by setting the AX scroll-bar value.
        # ProtonVPN's Mac Catalyst list ignores synthetic scroll-wheel events;
        # setting `value of scroll bar 1` is the only reliable way to scroll.
        # After scrolling, effective_slot is at the first visible row position.
        first_server_y = (
            r2_top + SERVER_ROW_H // 2 if already_expanded
            else r2_top + SERVER_ROW_H + SERVER_ROW_H // 2
        )
        visible_rows = max(1, (w_y + w_h - first_server_y) // SERVER_ROW_H) if w_h > 0 else 12
        if effective_slot > visible_rows and total_slots > 1:
            frac = min(1.0, max(0.0, (effective_slot - 1) / float(total_slots)))
            self.logger.log(
                f"Slot {slot_num} (eff {effective_slot}): scrolling to {frac:.5f} "
                f"({total_slots} states)",
                step="06", slot=slot_num,
            )
            set_scroll = f"""
            tell application "System Events"
                repeat with candidate in {{{process_list}}}
                    if exists process (candidate as text) then
                        tell process (candidate as text)
                            try
                                set value of scroll bar 1 of scroll area 1 of group 1 of window 1 to {frac:.6f}
                            end try
                        end tell
                        exit repeat
                    end if
                end repeat
            end tell
            """
            run_osascript(set_scroll, timeout=10,
                          label=f"scroll to slot {slot_num} (frac {frac:.4f})")
            time.sleep(0.6)
            # effective_slot is now the first visible state
            server_y = first_server_y

        # New Mac: the three-dot (⋯) button is always visible — no hover dance needed.
        # The state-name button is 255px wide; three-dot spans the full 303px row
        # width. Clicking 60px from the window's right edge lands in the zone
        # exclusive to the three-dot button (past the state-name portion).
        three_dot_x = w_x + w_w - 60
        Quartz.CGAssociateMouseAndMouseCursorPosition(True)
        _mouse(Quartz.kCGEventLeftMouseDown, three_dot_x, server_y)
        time.sleep(0.1)
        _mouse(Quartz.kCGEventLeftMouseUp, three_dot_x, server_y)
        self.logger.log(
            f"Slot {slot_num} (eff {effective_slot}): clicked three-dot at "
            f"({three_dot_x},{server_y})",
            step="06", slot=slot_num,
        )

        # ProtonVPN UI: clicking the ⋯ button opens a per-state IP popup.
        # Down selects the first IP in the list; Enter connects to it.
        time.sleep(0.8)
        _key(0x7D, True); _key(0x7D, False)   # Down → first IP
        time.sleep(0.35)
        _key(0x24, True); _key(0x24, False)   # Enter → connect
        time.sleep(0.5)
        return "connect_button_clicked"

    def _record_ip(self, ip: str | None) -> None:
        if not ip:
            return
        self.state.data["last_public_ip"] = ip
        if ip not in self.state.data["used_public_ips"]:
            self.state.data["used_public_ips"].append(ip)
        self.state.save()

    def _click_disconnect(self, app_name: str) -> str:
        process_list = self._process_name_candidates(app_name)
        # Avoid deep tree walk (which hits the 6000+ row server table and times out).
        # Try the Disconnect button at shallow known paths only.
        script = f"""
        tell application "System Events"
            set procName to ""
            repeat with candidate in {{{process_list}}}
                if exists process (candidate as text) then
                    set procName to candidate as text
                    exit repeat
                end if
            end repeat
            if procName is "" then return "vpn_process_not_found"

            tell process procName
                set frontmost to true
                delay 0.4
                if not (exists window 1) then return "no_window"

                -- Try "Disconnect" and "Cancel" (shown during Connecting state)
                -- at window and two group levels.  Skip scroll areas to avoid
                -- hitting the 6000+ row server table which causes timeouts.
                set targetBtns to {{"Disconnect", "Cancel"}}
                repeat with btnName in targetBtns
                    try
                        if exists button (btnName as text) of window 1 then
                            click button (btnName as text) of window 1
                            return "disconnect_clicked"
                        end if
                    end try
                    try
                        repeat with g in groups of window 1
                            if exists button (btnName as text) of g then
                                click button (btnName as text) of g
                                return "disconnect_clicked"
                            end if
                            try
                                repeat with gg in groups of g
                                    if exists button (btnName as text) of gg then
                                        click button (btnName as text) of gg
                                        return "disconnect_clicked"
                                    end if
                                end repeat
                            end try
                        end repeat
                    end try
                end repeat

                return "no_disconnect_button"
            end tell
        end tell
        """
        return run_osascript(script, timeout=10, label=f"{app_name} disconnect")

    def _click_quick_connect(self, app_name: str) -> str:
        process_list = self._process_name_candidates(app_name)
        script = f"""
        on clickNamedButton(containerElem, btnName)
            tell application "System Events"
                try
                    if exists button btnName of containerElem then
                        click button btnName of containerElem
                        return true
                    end if
                end try
            end tell
            return false
        end clickNamedButton

        tell application "System Events"
            set procName to ""
            repeat with candidate in {{{process_list}}}
                if exists process (candidate as text) then
                    set procName to candidate as text
                    exit repeat
                end if
            end repeat
            if procName is "" then return "vpn_process_not_found"

            tell process procName
                set frontmost to true
                delay 0.4
                if not (exists window 1) then return "no_window"

                if my clickNamedButton(window 1, "Quick Connect") then return "quick_connect_clicked"
                if my clickNamedButton(window 1, "Connect") then return "connect_clicked"

                try
                    repeat with g in groups of window 1
                        if my clickNamedButton(g, "Quick Connect") then return "quick_connect_clicked"
                        if my clickNamedButton(g, "Connect") then return "connect_clicked"
                        try
                            repeat with gg in groups of g
                                if my clickNamedButton(gg, "Quick Connect") then return "quick_connect_clicked"
                                if my clickNamedButton(gg, "Connect") then return "connect_clicked"
                            end repeat
                        end try
                    end repeat
                end try

                return "quick_connect_not_found"
            end tell
        end tell
        """
        return run_osascript(script, timeout=10, label=f"{app_name} quick connect")

    def _read_ui_connection_state(self, app_name: str) -> str:
        """Best-effort UI status for logs only.

        Network verification is authoritative. If UI text is used for human
        diagnostics, negative states must be checked before positive words so
        "You are not connected" can never become "connected".
        """
        process_list = self._process_name_candidates(app_name)
        script = f"""
        tell application "System Events"
            set procName to ""
            repeat with candidate in {{{process_list}}}
                if exists process (candidate as text) then
                    set procName to candidate as text
                    exit repeat
                end if
            end repeat
            if procName is "" then return "vpn_process_not_found"

            tell process procName
                if not (exists window 1) then return "no_window"
                set txt to ""
                try
                    repeat with s in static texts of window 1
                        try
                            set txt to txt & " " & (value of s as text)
                        end try
                        try
                            set txt to txt & " " & (name of s as text)
                        end try
                    end repeat
                end try
                try
                    repeat with b in buttons of window 1
                        try
                            set txt to txt & " " & (name of b as text)
                        end try
                    end repeat
                end try
                return txt
            end tell
        end tell
        """
        try:
            raw = run_osascript(script, timeout=8, label=f"{app_name} ui status")
        except AutomationError as exc:
            return f"unknown:{exc}"
        return self._classify_ui_connection_text(raw)

    @staticmethod
    def _classify_ui_connection_text(raw: str) -> str:
        lowered = " ".join(raw.lower().split())
        if not lowered:
            return "unknown_empty_ui"
        negative_markers = (
            "not connected",
            "you are not connected",
            "quick connect",
            "connect now",
            "disconnected",
        )
        if any(marker in lowered for marker in negative_markers):
            return "ui_disconnected"
        positive_markers = (
            "protected",
            "connected",
            "disconnect",
        )
        if any(marker in lowered for marker in positive_markers):
            return "ui_connected_hint"
        return "ui_unknown"

    def _poll_verify(
        self,
        baseline_ip: str | None,
        target_cc: str,
        provider_token: str,
        require_provider_in_org: bool,
        verify_timeout: int,
        baseline_route: str = "",
    ) -> str:
        """Local-first VPN verification.

        Levels (evaluated each poll):
          L1 — utun interface active (ifconfig)
          L2 — default route changed from baseline (route get default) — default success
          L3 — public IP changed (optional, degrades gracefully on 429)
          L4 — country + optional org match (optional, degrades gracefully on 429)

        L2 is the minimum condition for "connected".
        L3/L4 are attempted but never cause a failure if APIs are rate-limited.
        """
        deadline = time.monotonic() + verify_timeout
        attempts = 0
        last_tunnels: list[str] = []
        last_ip: str | None = None
        country_check: str = "pending"  # pending | verified | rate_limited | wrong

        while time.monotonic() < deadline:
            attempts += 1
            time.sleep(1)

            # ── Level 1: tunnel interface ──────────────────────────────────────
            tunnels = self.net.active_tunnel_interfaces()
            has_tunnel = bool(tunnels)

            if not has_tunnel:
                self.logger.log(
                    f"VPN L1 pending: no active utun (attempt {attempts})",
                    step="06", status="vpn_pending_no_tunnel", attempt=attempts,
                )
                last_tunnels = []
                continue

            # ── Level 2: route changed ─────────────────────────────────────────
            current_route = self.net.default_route_gateway()
            # Accept three conditions: route changed to new value, OR route is gone
            # (ProtonVPN kill-switch drops the default route while tunnel is active).
            # Baseline may also be empty on cycle 2+ if previous VPN left no default route.
            route_changed = (
                (bool(current_route) and current_route != baseline_route)
                or (has_tunnel and not current_route)
            )

            if not route_changed:
                self.logger.log(
                    f"VPN L2 pending: route unchanged (gw={current_route} baseline={baseline_route}) "
                    f"tunnels={tunnels} attempt={attempts}",
                    step="06", status="vpn_pending_route_unchanged", attempt=attempts,
                )
                last_tunnels = tunnels
                continue

            # L1 + L2 satisfied — VPN is connected at minimum level
            verify_level = "tunnel+route"

            # ── Level 3: public IP changed (optional) ─────────────────────────
            ip_info = self.net.public_ip_info()
            ip = (ip_info or {}).get("ip")
            if ip:
                last_ip = ip
                if baseline_ip and ip == baseline_ip:
                    # IP hasn't changed yet — wait a bit more (up to half the budget)
                    if attempts < verify_timeout // 2:
                        self.logger.log(
                            f"VPN L3 pending: IP unchanged from baseline ({ip}) attempt={attempts}",
                            step="06", status="vpn_pending_ip_unchanged", attempt=attempts,
                        )
                        continue
                    # Beyond halfway — accept L2 result
                    self.logger.log(
                        f"VPN L3 skipped: IP unchanged after {attempts}s — accepting L2 result",
                        step="06", status="vpn_l3_skipped_ip_stale",
                    )
                else:
                    verify_level = "tunnel+route+ip"

                # ── Level 4: country (optional) ───────────────────────────────
                info = {"ip": ip, "country": (ip_info or {}).get("country"), "org": (ip_info or {}).get("org")}
                if self._is_connected_to(info, target_cc, require_provider_in_org, provider_token):
                    verify_level = "tunnel+route+ip+country"
                    country_check = "verified"
                else:
                    country_check = "wrong"
                    if require_provider_in_org:
                        self.logger.log(
                            f"VPN L4 wrong: ip={ip} country={info.get('country')} org={info.get('org')} "
                            f"wanted={target_cc} attempt={attempts}",
                            step="06", status="vpn_pending_wrong_country", attempt=attempts,
                        )
                        continue
                    # require_provider_in_org=False: country mismatch is tolerated at L2
                    self.logger.log(
                        f"VPN L4 country mismatch (tolerated): ip={ip} country={info.get('country')} "
                        f"wanted={target_cc} — accepting L2 result",
                        step="06", status="vpn_l4_country_mismatch_tolerated",
                    )
            else:
                # Both APIs rate-limited — accept L2 result
                country_check = "rate_limited"
                self.logger.log(
                    f"VPN L3/L4 skipped: both IP APIs rate-limited — accepted at {verify_level}",
                    step="06", status="vpn_api_rate_limited_accept_l2",
                )

            # ── Accept ────────────────────────────────────────────────────────
            self._record_ip(last_ip)
            self.state.data["vpn_verify_level"] = verify_level
            self.state.save()
            self.logger.log(
                f"VPN connected: level={verify_level} tunnels={tunnels} route={current_route} "
                f"ip={last_ip} country_check={country_check} after {attempts}s",
                step="06", status="connected_verified",
                verify_level=verify_level, tunnels=tunnels, ip=last_ip, attempts=attempts,
            )
            return "connected_verified"

        raise AutomationError(
            f"VPN verification failed after {verify_timeout}s. "
            f"tunnels={last_tunnels} baseline_route={baseline_route} ip={last_ip} "
            f"wanted={target_cc}"
        )

    def diagnose_current_state(self, vpn_cfg: VPNConfig) -> dict[str, Any]:
        provider_token = self._provider_token(vpn_cfg.app)
        snapshot = self.net.snapshot()
        ui_state = self._read_ui_connection_state(vpn_cfg.app)
        info = {
            "ip": snapshot.get("public_ip"),
            "country": snapshot.get("country"),
            "org": snapshot.get("org"),
        }
        network_matches = self._is_connected_to(
            info, vpn_cfg.location_code, vpn_cfg.require_provider_in_org, provider_token,
        )
        verified = bool(snapshot.get("has_tunnel_interface") and network_matches)
        result = {
            "verified_connected": verified,
            "ui_connection_state": ui_state,
            "network": snapshot,
            "target_country": vpn_cfg.location_code,
            "provider_token": provider_token,
            "require_provider_in_org": vpn_cfg.require_provider_in_org,
            "reason": "connected_verified" if verified else "not_connected_verified_by_network",
        }
        self.state.data["last_vpn_diagnostic"] = result
        self.state.save()
        return result


# -----------------------------------------------------------------------------
# Chrome Controller
# -----------------------------------------------------------------------------
class ChromeController:
    def __init__(self, logger: RunLogger, state: StateManager):
        self.logger = logger
        self.state = state

    def activate(self) -> None:
        if HAS_PYXA:
            try:
                PyXA.Application("Google Chrome").activate()
                return
            except Exception:
                pass
        run_osascript('tell application "Google Chrome" to activate', label="activate Chrome")

    def enumerate_tabs(self) -> dict[str, dict[str, str]]:
        script = """
        tell application "Google Chrome"
            activate
            if (count of windows) is 0 then return ""
            set tabData to ""
            tell front window
                set n to count of tabs
                repeat with i from 1 to n
                    set tabTitle to title of tab i
                    set tabUrl to URL of tab i
                    set tabData to tabData & (i as text) & "<<|>>" & tabTitle & "<<|>>" & tabUrl & linefeed
                end repeat
            end tell
            return tabData
        end tell
        """
        out = run_osascript(script, timeout=15, label="enumerate Chrome tabs")
        cache: dict[str, dict[str, str]] = {}
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        for line in out.splitlines():
            parts = line.split("<<|>>")
            if len(parts) >= 3:
                cache[parts[0].strip()] = {
                    "title": parts[1],
                    "url": parts[2],
                    "detected_at": now,
                }
        self.state.update(chrome_tabs_cache=cache)
        return cache

    def switch_tab(self, tab_no: int) -> tuple[str, str]:
        script = """
        tell application "Google Chrome"
            activate
            if (count of windows) is 0 then error "No Chrome windows"
            tell front window
                if __TAB__ > (count of tabs) then error "Tab __TAB__ does not exist"
                set active tab index to __TAB__
                delay 0.3
                set t to title of active tab
                set u to URL of active tab
            end tell
        end tell
        return t & linefeed & u
        """.replace("__TAB__", str(tab_no))
        out = run_osascript(script, timeout=10, label=f"switch tab {tab_no}")
        parts = out.splitlines()
        title = parts[0] if parts else ""
        url = parts[1] if len(parts) > 1 else ""
        return title, url


# -----------------------------------------------------------------------------
# Podcasts Controller
# -----------------------------------------------------------------------------
class PodcastsController:
    def __init__(self, logger: RunLogger, state: StateManager):
        self.logger = logger
        self.state = state

    def open_url(self, url: str) -> None:
        subprocess.run(["open", "-a", "Podcasts", url], check=True)

    def activate(self) -> None:
        if HAS_PYXA:
            try:
                PyXA.Application("Podcasts").activate()
                return
            except Exception:
                pass
        run_osascript('tell application "Podcasts" to activate', label="activate Podcasts")

    def wait_for_window(self, timeout_sec: int = 20) -> None:
        script = """
        tell application "Podcasts" to activate
        tell application "System Events"
            set deadline to (current date) + __TIMEOUT__
            repeat while (current date) < deadline
                if exists process "Podcasts" then
                    tell process "Podcasts"
                        if exists window 1 then return "ready"
                    end tell
                end if
                delay 0.5
            end repeat
        end tell
        error "Podcasts window did not appear within __TIMEOUT__ s"
        """.replace("__TIMEOUT__", str(timeout_sec))
        run_osascript(script, timeout=timeout_sec + 5, label="wait for Podcasts window")

    def capture_show_name(self) -> str:
        """Read the podcast show name from the current Podcasts window.

        Tries window title first, then the first prominent heading in the AX tree.
        Returns a non-empty string or 'unknown_show'.
        """
        script = """
        tell application "System Events"
            tell process "Podcasts"
                if not (exists window 1) then return "unknown_show"
                -- Window title often contains the show name (e.g. "My Podcast – Podcasts")
                set wTitle to ""
                try
                    set wTitle to name of window 1 as string
                end try
                if wTitle is not "" and wTitle is not "Podcasts" then
                    return wTitle
                end if
                -- Fall back: first static text with value length > 4 in content area
                set wPos to position of window 1
                set contentLeft to (item 1 of wPos) + 180
                set q to {window 1}
                set deadline to (current date) + 5
                repeat 400 times
                    if (count of q) = 0 then exit repeat
                    if (current date) > deadline then exit repeat
                    set elem to item 1 of q
                    if (count of q) > 1 then
                        set q to items 2 thru -1 of q
                    else
                        set q to {}
                    end if
                    set eRole to ""
                    try
                        set eRole to role of elem as string
                    end try
                    if eRole is "AXStaticText" then
                        set eVal to ""
                        try
                            set eVal to value of elem as string
                        end try
                        if length of eVal > 4 then
                            try
                                set ePos to position of elem
                                if (item 1 of ePos) > contentLeft then
                                    return eVal
                                end if
                            end try
                        end if
                    end if
                    try
                        repeat with ch in UI elements of elem
                            set end of q to ch
                        end repeat
                    end try
                end repeat
                return "unknown_show"
            end tell
        end tell
        """
        try:
            raw = run_osascript(script, timeout=12, label="capture show name")
            # Strip " – Podcasts" suffix that appears in the window title
            name = raw.strip()
            for suffix in (" – Podcasts", " - Podcasts", " — Podcasts"):
                if name.endswith(suffix):
                    name = name[: -len(suffix)].strip()
            return name if name and name != "unknown_show" else "unknown_show"
        except AutomationError:
            return "unknown_show"

    def click_see_all(self, time_budget_sec: int = DEFAULT_SEE_ALL_BUDGET_SEC) -> str:
        """Find and click the episode-list 'See All' via the native AX walk (~1s/pass).

        Returns 'clicked' | 'list_already_expanded:native' | 'see_all_not_found'.
        The old System Events version re-walked the deep tree on every poll (~18-20s,
        sometimes the full 60s budget); this polls the fast native snapshot instead.
        Falls back to the System Events walk if the native path can't resolve it.
        """
        try:
            import Quartz  # type: ignore[import]
        except ImportError:
            return self._click_see_all_sysevents(time_budget_sec)

        try:
            run_osascript('tell application "Podcasts" to activate',
                          timeout=5, label="activate before See All")
        except AutomationError:
            pass

        def _click(cx: int, cy: int) -> None:
            for k in (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDown,
                      Quartz.kCGEventLeftMouseUp):
                ev = Quartz.CGEventCreateMouseEvent(
                    None, k, Quartz.CGPointMake(cx, cy), Quartz.kCGMouseButtonLeft)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                time.sleep(0.08)

        def _episode_count(nodes) -> int:
            return sum(1 for role, _t, x, y, w, h in nodes
                       if role == "AXButton" and h > 60 and w > 400)

        deadline = time.time() + time_budget_sec
        while time.time() < deadline:
            nodes = self._ax_nodes()
            # Content area starts past the sidebar; the real episode-list 'See All' is
            # an AXButton there. EXCLUDE menu roles — the macOS menu bar contains an
            # Apple-menu "Show All" item that would otherwise match (and is at the far
            # left). Pick the topmost content-area button (above the recommendation
            # carousels' own See All).
            win_x = 0
            for role, _t, x, y, w, h in nodes:
                if role == "AXWindow" and w > 400 and h > 400:
                    win_x = x
                    break
            content_left = win_x + 180
            # Podcasts shows an "Episodes" button to navigate to the full episode list.
            cands = sorted(
                ((x + w // 2, y + h // 2) for role, t, x, y, w, h in nodes
                 if role in ("AXButton", "AXRadioButton", "AXTab", "AXCell") and w > 0
                 and t.startswith(("Episodes", "All Episodes"))),
                key=lambda c: c[1],
            )
            if cands:
                before = _episode_count(nodes)
                # Only the TOPMOST See All — that's the episodes section; carousel
                # 'See All's sit lower and would navigate away from the show.
                cx, cy = cands[0]
                _click(cx, cy)
                time.sleep(1.2)
                after = _episode_count(self._ax_nodes())
                if after >= 1 and after >= before:
                    self.logger.log(
                        f"See All (native): clicked at ({cx},{cy}); "
                        f"episode rows {before}->{after}", step="11",
                    )
                    return "clicked"
            elif _episode_count(nodes) >= 1:
                # No See All but episodes are already on screen (short shows).
                self.logger.log("See All (native): list already shows episodes", step="11")
                return "list_already_expanded:native"
            time.sleep(0.5)

        self.logger.log("See All (native): not found in budget — System Events fallback",
                        step="11")
        return self._click_see_all_sysevents(time_budget_sec)

    def _click_see_all_sysevents(self, time_budget_sec: int = DEFAULT_SEE_ALL_BUDGET_SEC) -> str:
        """Fallback: System Events tree walk for 'See All' (~18-60s). Kept as a safety
        net for click_see_all.
        """
        script = _BOUNDED_HELPERS + """
        on matchesSeeAll(s)
            if s is missing value then return false
            try
                set t to s as text
            on error
                return false
            end try
            if t is "" then return false
            if t is "Episodes" then return true
            if t is "All Episodes" then return true
            if t contains "Episodes" then return true
            return false
        end matchesSeeAll

        on findSeeAllElement(rootElem, maxDepth, deadline)
            tell application "System Events"
                set stack to {{rootElem, 0}}
                repeat while (count of stack) > 0
                    -- Abort if we have exceeded the outer deadline so the caller
                    -- can return quickly instead of hanging until osascript timeout.
                    if (current date) > deadline then return missing value
                    set lastPair to item -1 of stack
                    if (count of stack) > 1 then
                        set stack to items 1 thru -2 of stack
                    else
                        set stack to {}
                    end if
                    set elem to item 1 of lastPair
                    set d to item 2 of lastPair

                    set nn to ""
                    try
                        set nn to name of elem
                    end try
                    set dd to ""
                    try
                        set dd to description of elem
                    end try
                    set vv to ""
                    try
                        set vv to (value of elem) as text
                    end try

                    if (my matchesSeeAll(nn)) or (my matchesSeeAll(dd)) or (my matchesSeeAll(vv)) then
                        return elem
                    end if

                    if d < maxDepth then
                        try
                            repeat with child in UI elements of elem
                                set end of stack to {child, d + 1}
                            end repeat
                        end try
                    end if
                end repeat
            end tell
            return missing value
        end findSeeAllElement

        on attemptClick(elem)
            tell application "System Events"
                try
                    click elem
                    return "click_ok"
                end try
                try
                    perform action "AXPress" of elem
                    return "axpress_ok"
                end try
                try
                    set p to value of attribute "AXParent" of elem
                    if p is not missing value then
                        try
                            click p
                            return "parent_click_ok"
                        end try
                        try
                            perform action "AXPress" of p
                            return "parent_axpress_ok"
                        end try
                    end if
                end try
            end tell
            return "click_failed"
        end attemptClick

        tell application "Podcasts" to activate
        delay 0.3
        tell application "System Events"
            tell process "Podcasts"
                set frontmost to true
                delay 0.2
                if not (exists window 1) then return "no_window"

                -- Retry for up to __BUDGET__ seconds; Podcasts can take 15-30 s to render
                -- the show page and the "See All" element after a URL open via VPN.
                -- Pass deadline into findSeeAllElement so it aborts if one BFS pass runs
                -- long (avoids hanging past the osascript timeout).
                set deadline to (current date) + __BUDGET__
                repeat
                    set elem to my findSeeAllElement(window 1, __DEPTH__, deadline)
                    if elem is not missing value then
                        set clickResult to my attemptClick(elem)
                        if clickResult is "click_failed" then return "see_all_click_failed"
                        delay 0.3
                        return "clicked"
                    end if
                    if (current date) > deadline then return "see_all_not_found"
                    delay 0.25
                end repeat
            end tell
        end tell
        """.replace("__DEPTH__", str(DEFAULT_ACCESSIBILITY_DEPTH)
                    ).replace("__BUDGET__", str(DEFAULT_SEE_ALL_BUDGET_SEC))
        return run_osascript(script, timeout=time_budget_sec + 20, label="click See All")

    def episode_list_state(self, min_rows: int = 1) -> str:
        """Detect whether a plausible episode list is already visible.

        This is deliberately bounded and conservative. It is used only to decide
        whether missing "See All" can be treated as already expanded.
        """
        script = _BOUNDED_HELPERS + """
        on collectRows(rootElem, maxDepth)
            tell application "System Events"
                set candidates to {}
                set stack to {{rootElem, 0}}
                repeat while (count of stack) > 0
                    set lastPair to item -1 of stack
                    if (count of stack) > 1 then
                        set stack to items 1 thru -2 of stack
                    else
                        set stack to {}
                    end if
                    set elem to item 1 of lastPair
                    set d to item 2 of lastPair
                    try
                        repeat with r in rows of elem
                            set t to my textOfElement(r)
                            if length of t > 20 then set end of candidates to t
                        end repeat
                    end try
                    try
                        repeat with g in groups of elem
                            set t to my textOfElement(g)
                            if length of t > 20 then set end of candidates to t
                        end repeat
                    end try
                    if d < maxDepth then
                        try
                            repeat with child in UI elements of elem
                                set end of stack to {child, d + 1}
                            end repeat
                        end try
                    end if
                end repeat
                return candidates
            end tell
        end collectRows

        tell application "Podcasts" to activate
        tell application "System Events"
            tell process "Podcasts"
                if not (exists window 1) then return "no_window"
                set rowsFound to my collectRows(window 1, 5)
                set rowCount to count of rowsFound
                if rowCount >= __MIN_ROWS__ then
                    return "list_already_expanded:" & rowCount
                end if
                return "episode_list_not_visible:" & rowCount
            end tell
        end tell
        """.replace("__MIN_ROWS__", str(min_rows))
        return run_osascript(script, timeout=15, label="detect episode list")

    def scroll_to_top(self) -> None:
        # Cmd+Up scrolls to top of the current focused scroll view without
        # navigating away.  key code 115 (Home) triggers Podcasts' main nav
        # and sends the app back to the Listen Now screen.
        script = """
        tell application "System Events"
            tell process "Podcasts"
                set frontmost to true
                key code 126 using command down
                delay 0.2
            end tell
        end tell
        """
        try:
            run_osascript(script, timeout=5, label="scroll to top")
        except AutomationError:
            pass  # non-fatal — window may not be focused

    def download_episode_row(self, video_no: int) -> str:
        """Click the download (↓) button for the Nth episode.

        The download button is hover-only — absent from the AX tree until the
        mouse physically hovers the row.  Strategy:
          1. BFS to find the Nth episode button and read its pixel rect.
          2. Navigate into it to find the 'more' (⋯) button center.
          3. Quartz: move mouse to row center → pause for hover state → click at
             (more_x - 35, more_y), which is where the download icon sits.
        """
        # Reset scroll to top before each BFS — episode list uses lazy rendering
        # so AX only exposes the currently visible rows. Without this, the row
        # counter is relative to the current scroll position, not the episode number.
        self.scroll_to_top()
        # CMD+Up alone is unreliable when a prior download used AXScrollDownByPage
        # (Mac Catalyst's synthetic scroll), so supplement with AXScrollUpByPage until
        # the topmost visible button reaches y >= 130 (ep1 at the list head).
        try:
            from ApplicationServices import (  # type: ignore[import]
                AXUIElementCreateApplication, AXUIElementCopyAttributeValue,
                AXUIElementPerformAction, kAXChildrenAttribute, kAXRoleAttribute,
                kAXPositionAttribute, kAXSizeAttribute, AXValueGetValue,
                kAXValueCGPointType, kAXValueCGSizeType,
            )
            _top_pid = int(subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to return unix id of process "Podcasts"'],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip())
            _top_ax = AXUIElementCreateApplication(_top_pid)

            def _scan_ep_btns(root):
                stack = [root]; seen = 0
                while stack and seen < 8000:
                    el = stack.pop(); seen += 1
                    _, role = AXUIElementCopyAttributeValue(el, kAXRoleAttribute, None)
                    if role == "AXButton":
                        _, pv = AXUIElementCopyAttributeValue(el, kAXPositionAttribute, None)
                        _, sv = AXUIElementCopyAttributeValue(el, kAXSizeAttribute, None)
                        if pv and sv:
                            _, pt = AXValueGetValue(pv, kAXValueCGPointType, None)
                            _, sz = AXValueGetValue(sv, kAXValueCGSizeType, None)
                            try:
                                if int(sz.height) > 60 and int(sz.width) > 400:
                                    yield el, int(pt.y)
                            except (OverflowError, ValueError):
                                pass
                    _, ch = AXUIElementCopyAttributeValue(el, kAXChildrenAttribute, None)
                    if ch: stack.extend(ch)

            time.sleep(0.3)
            for _ in range(20):
                _rows = sorted(_scan_ep_btns(_top_ax), key=lambda r: r[1])
                if not _rows:
                    break
                if _rows[0][1] >= 130:
                    break
                _up_el = next((el for el, ey in _rows if ey >= 100), _rows[0][0])
                AXUIElementPerformAction(_up_el, "AXScrollUpByPage")
                time.sleep(0.4)
        except Exception:
            pass

        # Phase 1: AppleScript BFS — locate the Nth episode and its more-button center.
        script = f"""
        tell application "System Events"
            set frontmost of process "Podcasts" to true
        end tell
        delay 0.3
        tell application "System Events"
            tell process "Podcasts"
                if not (exists window 1) then return "ERROR:no_window"

                set targetN to {video_no}
                set seenCount to 0
                set targetEp to missing value
                set queue to {{window 1}}

                repeat 3000 times
                    if (count of queue) = 0 then exit repeat

                    set elem to item 1 of queue
                    if (count of queue) > 1 then
                        set queue to items 2 thru -1 of queue
                    else
                        set queue to {{}}
                    end if

                    set isBtn to false
                    try
                        if class of elem is button then set isBtn to true
                    end try
                    if isBtn then
                        -- Structural filter: episode rows are tall (>60px) and wide (>400px).
                        -- This is locale-independent and robust to date-format changes.
                        set looksLikeEpisode to false
                        try
                            set eSz to size of elem
                            set btnH to (item 2 of eSz) as integer
                            set btnW to (item 1 of eSz) as integer
                            if btnH > 60 and btnW > 400 then
                                set looksLikeEpisode to true
                            end if
                        end try
                        if looksLikeEpisode then
                            set seenCount to seenCount + 1
                            if seenCount = targetN then
                                set targetEp to elem
                                exit repeat
                            end if
                        end if
                    end if

                    try
                        repeat with ch in UI elements of elem
                            set end of queue to ch
                        end repeat
                    end try
                end repeat

                if targetEp is missing value then
                    return "ERROR:episode_not_found|seen=" & seenCount
                end if

                -- Read episode rect
                set ePos to position of targetEp
                set eSz to size of targetEp
                set eX to (item 1 of ePos) as integer
                set eY to (item 2 of ePos) as integer
                set eW to (item 1 of eSz) as integer
                set eH to (item 2 of eSz) as integer

                -- Find 'more' button center: episode → (optional group) → more
                set moreX to 0
                set moreY to 0
                try
                    repeat with k in UI elements of targetEp
                        set kd to ""
                        try
                            set kd to description of k as string
                        end try
                        if kd is "more" then
                            set mp to position of k
                            set ms to size of k
                            set moreX to ((item 1 of mp) + (item 1 of ms) / 2) as integer
                            set moreY to ((item 2 of mp) + (item 2 of ms) / 2) as integer
                            exit repeat
                        end if
                        -- one level deeper (group → more)
                        try
                            repeat with gk in UI elements of k
                                set gkd to ""
                                try
                                    set gkd to description of gk as string
                                end try
                                if gkd is "more" then
                                    set mp to position of gk
                                    set ms to size of gk
                                    set moreX to ((item 1 of mp) + (item 1 of ms) / 2) as integer
                                    set moreY to ((item 2 of mp) + (item 2 of ms) / 2) as integer
                                    exit repeat
                                end if
                            end repeat
                        end try
                        if moreX > 0 then exit repeat
                    end repeat
                end try

                set wPos to position of window 1
                set wSz to size of window 1
                set wY to (item 2 of wPos) as integer
                set wH to (item 2 of wSz) as integer
                return "WIN:" & wY & "," & wH & "|ROW:" & eX & "," & eY & "," & eW & "," & eH & "|MORE:" & moreX & "," & moreY
            end tell
        end tell
        """

        out = run_osascript(script, timeout=90, label=f"find episode {video_no} position")

        # Episode not visible in the initial BFS — scroll the list using AXScrollDownByPage.
        # Mac Catalyst ignores CGEventCreateScrollWheelEvent and does not respond to
        # osascript/Quartz Page Down key events for its episode list.  However, every
        # visible episode AXButton exposes AXScrollDownByPage as an AX action, and
        # performing it reliably scrolls the list by one viewport.
        #
        # After each scroll we compare row titles to count exactly how many rows
        # scrolled off the top (some overlap rows stay in the AX tree at negative y
        # coordinates), so the adjusted BFS target is always precise.
        if out.startswith("ERROR:episode_not_found"):
            # Mac Catalyst ignores CGEventCreateScrollWheelEvent and all synthetic
            # Page Down inputs.  Strategy: walk the AX tree to get a direct element
            # reference for an episode AXButton, perform AXScrollDownByPage on it
            # (NOT via AXUIElementCopyElementAtPosition which returns the innermost
            # child — an AXImage/AXStaticText — that lacks the scroll action), then
            # accumulate rows until we reach video_no.
            try:
                from ApplicationServices import (  # type: ignore[import]
                    AXUIElementCreateApplication,
                    AXUIElementCopyAttributeValue,
                    AXValueGetValue,
                    AXUIElementPerformAction,
                    kAXChildrenAttribute,
                    kAXRoleAttribute,
                    kAXPositionAttribute,
                    kAXSizeAttribute,
                    kAXDescriptionAttribute,
                    kAXValueAttribute,
                    kAXTitleAttribute,
                    kAXValueCGPointType,
                    kAXValueCGSizeType,
                )
                _pid = int(subprocess.run(
                    ["osascript", "-e",
                     'tell application "System Events" to return unix id of process "Podcasts"'],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip())
                _ax_app = AXUIElementCreateApplication(_pid)

                def _ax_attr(el, a):
                    try:
                        err, val = AXUIElementCopyAttributeValue(el, a, None)
                        return val if err == 0 else None
                    except Exception:
                        return None

                def _ep_button_walk(root):
                    """DFS walk — yields (el_ref, y, x, w, h, text) for episode AXButtons."""
                    stack = [root]; seen = 0
                    while stack and seen < 15000:
                        el = stack.pop(); seen += 1
                        role = _ax_attr(el, kAXRoleAttribute)
                        if role == "AXButton":
                            pv = _ax_attr(el, kAXPositionAttribute)
                            sv = _ax_attr(el, kAXSizeAttribute)
                            if pv and sv:
                                _, pt = AXValueGetValue(pv, kAXValueCGPointType, None)
                                _, sz = AXValueGetValue(sv, kAXValueCGSizeType, None)
                                try:
                                    w2, h2 = int(sz.width), int(sz.height)
                                    x2, y2 = int(pt.x), int(pt.y)
                                except (OverflowError, ValueError):
                                    continue
                                if h2 > 60 and w2 > 400:
                                    txt = ""
                                    for _a in (kAXDescriptionAttribute, kAXValueAttribute,
                                               kAXTitleAttribute):
                                        v = _ax_attr(el, _a)
                                        if isinstance(v, str) and v:
                                            txt = v; break
                                    yield (el, y2, x2, w2, h2, txt[:60])
                        ch = _ax_attr(el, kAXChildrenAttribute)
                        if ch:
                            stack.extend(ch)

                def _ep_rows_sorted():
                    return sorted(_ep_button_walk(_ax_app), key=lambda r: r[1])

                # Reset to true top so accumulation starts from episode 1.
                # CMD+Up alone may not fully reset Mac Catalyst's list when it was
                # previously scrolled for an earlier episode (AX tree retains old rows).
                # AXScrollUpByPage loop ensures we reach the actual beginning.
                self.scroll_to_top()
                time.sleep(0.5)
                # Scroll up until ep1 is stably at the content-area top (y ≈ 157).
                # We stop when the topmost AX element has y >= 130 — that element
                # must be ep1 because any earlier element would also be in the tree.
                # We call AXScrollUpByPage on the first VISIBLE element (y >= 100)
                # because calling it on an off-screen element (y < 0) barely moves.
                _su_top_y: int = -9999
                for _su in range(20):
                    _top_scan = list(_ep_rows_sorted())
                    if not _top_scan:
                        break
                    _su_top_y = _top_scan[0][1]
                    if _su_top_y >= 130:
                        break  # ep1 is at the content-area top — truly at the beginning
                    # Use first visible element for effective scrolling
                    _scroll_el = None
                    for _su_el, _su_y, _su_x, _su_w, _su_h, _ in _top_scan:
                        if _su_y >= 100:
                            _scroll_el = _su_el
                            break
                    if _scroll_el is None:
                        _scroll_el = _top_scan[0][0]
                    AXUIElementPerformAction(_scroll_el, "AXScrollUpByPage")
                    time.sleep(0.4)

                # Get win_y / win_h for _click_download_at.
                _win_y = _win_h = 0
                for _r, _t, _x, _y, _w, _h in self._ax_nodes():
                    if _r == "AXWindow" and _w > 400 and _h > 400:
                        _win_y, _win_h = _y, _h
                        break

                # Accumulate unique episode rows in order from the top.
                # Each row stores (y, x, w, h) from the scan where it FIRST appeared.
                # We stop once we have ≥ video_no rows; the target row was just added
                # in the most-recent scan so its y is its current on-screen position.
                accumulated: list[tuple[int, int, int, int, str]] = []
                seen_titles: set[str] = set()

                def _absorb_walk():
                    added = 0
                    for _el, _y, _x, _w, _h, _title in _ep_rows_sorted():
                        if _title not in seen_titles:
                            seen_titles.add(_title)
                            accumulated.append((_y, _x, _w, _h, _title))
                            added += 1
                    return added

                _absorb_walk()
                self.logger.log(
                    f"Download episode {video_no}: AX scroll start — "
                    f"{len(accumulated)} initial rows (top={_su_top_y})",
                    step="13",
                )

                for _sa in range(30):
                    if len(accumulated) >= video_no:
                        break

                    # Get the first episode AXButton element reference directly from
                    # the AX walk (not via CopyElementAtPosition which returns a child).
                    _scroll_el = None
                    for _el, _y, _x, _w, _h, _title in _ep_rows_sorted():
                        _scroll_el = _el
                        break

                    if _scroll_el is None:
                        self.logger.log(
                            f"Download episode {video_no}: AX scroll #{_sa + 1} "
                            "— no scroll element found",
                            step="13",
                        )
                        break

                    _err_sc = AXUIElementPerformAction(_scroll_el, "AXScrollDownByPage")
                    time.sleep(0.5)
                    _added = _absorb_walk()

                    self.logger.log(
                        f"Download episode {video_no}: AXScrollDownByPage #{_sa + 1} "
                        f"err={_err_sc} new_rows={_added} total={len(accumulated)}",
                        step="13",
                    )

                    if _added == 0:
                        break  # end of list

                if len(accumulated) >= video_no:
                    _row_y, _row_x, _row_w, _row_h, _row_title = accumulated[video_no - 1]
                    _more_x = _row_x + _row_w - 47
                    _more_y = _row_y + _row_h // 2

                    # If the target row's click point is below the window bottom,
                    # the row entered the AX tree but isn't yet in the viewport.
                    # One extra AXScrollDownByPage brings it into view; then we
                    # refresh its y-coordinate by looking it up by title.
                    _win_bottom = _win_y + _win_h if _win_h > 0 else 9999
                    if _more_y >= _win_bottom:
                        _vis_rows = list(_ep_rows_sorted())
                        _extra_el = next(
                            (_el for _el, _y, _x, _w, _h, _ in _vis_rows if _y >= 100),
                            _vis_rows[0][0] if _vis_rows else None,
                        )
                        if _extra_el is not None:
                            AXUIElementPerformAction(_extra_el, "AXScrollDownByPage")
                            time.sleep(0.5)
                            for _el, _y, _x, _w, _h, _t in _ep_rows_sorted():
                                if _t == _row_title:
                                    _row_x, _row_y, _row_w, _row_h = _x, _y, _w, _h
                                    _more_x = _row_x + _row_w - 47
                                    _more_y = _row_y + _row_h // 2
                                    self.logger.log(
                                        f"Download episode {video_no}: extra scroll — "
                                        f"row refreshed to ({_row_x},{_row_y},{_row_w},{_row_h})",
                                        step="13",
                                    )
                                    break

                    self.logger.log(
                        f"Download episode {video_no}: AX scroll located row at "
                        f"({_row_x},{_row_y},{_row_w},{_row_h})",
                        step="13",
                    )
                    return self._click_download_at(
                        _win_y, _win_h,
                        _row_x, _row_y, _row_w, _row_h,
                        _more_x, _more_y,
                        video_no,
                    )
                else:
                    self.logger.log(
                        f"Download episode {video_no}: AX scroll collected only "
                        f"{len(accumulated)} rows (need {video_no})",
                        step="13",
                    )

            except Exception as _exc:
                self.logger.log(
                    f"Download episode {video_no}: AX scroll fallback error: {_exc}",
                    step="13",
                )

        if out.startswith("ERROR:"):
            self.logger.log(f"Download episode {video_no}: {out}", step="13")
            return "download_not_found"

        win_y = win_h = 0
        row_x = row_y = row_w = row_h = more_x = more_y = 0
        for chunk in out.split("|"):
            if chunk.startswith("WIN:"):
                parts = chunk[4:].split(",")
                if len(parts) == 2:
                    try:
                        win_y, win_h = int(parts[0]), int(parts[1])
                    except ValueError:
                        pass
            elif chunk.startswith("ROW:"):
                parts = chunk[4:].split(",")
                if len(parts) == 4:
                    try:
                        row_x, row_y, row_w, row_h = (
                            int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
                        )
                    except ValueError:
                        pass
            elif chunk.startswith("MORE:"):
                parts = chunk[5:].split(",")
                if len(parts) == 2:
                    try:
                        more_x, more_y = int(parts[0]), int(parts[1])
                    except ValueError:
                        pass

        if row_w == 0:
            self.logger.log(
                f"Download episode {video_no}: bad position in '{out}'", step="13"
            )
            self._dump_ax_tree(f"download_row_{video_no}_not_found")
            return "download_not_found"

        return self._click_download_at(
            win_y, win_h, row_x, row_y, row_w, row_h, more_x, more_y, video_no
        )

    def _find_episode_rows(
        self, max_n: int
    ) -> tuple[int, int, dict[int, tuple[int, int, int, int, int, int]]]:
        """Measure episode rows 1..max_n via the native AX walk (~1s).

        Episode rows are AXButtons that are tall (>60px) and wide (>400px); sorting
        the visible ones top-to-bottom gives episode 1, 2, ….  The hover-only ⋯
        button isn't in the tree (it appears only on physical hover), so its position
        is synthesized from the row rect (more_x = right edge − 47, vertical centre) —
        the same point the old System Events path reported.  Falls back to the
        System Events walk if the native walk sees no rows.
        """
        nodes = self._ax_nodes()
        win_y = win_h = 0
        for role, _text, x, y, w, h in nodes:
            if role == "AXWindow" and w > 400 and h > 400:
                win_y, win_h = y, h
                break
        eps = sorted(
            ((x, y, w, h) for role, _t, x, y, w, h in nodes
             if role == "AXButton" and h > 60 and w > 400),
            key=lambda r: r[1],
        )
        rows: dict[int, tuple[int, int, int, int, int, int]] = {}
        for i, (x, y, w, h) in enumerate(eps, start=1):
            if i > max_n:
                break
            more_x = x + w - 47       # ⋯ button centre (hover-only; from measured geometry)
            more_y = y + h // 2
            rows[i] = (x, y, w, h, more_x, more_y)
        if rows:
            self.logger.log(
                f"_find_episode_rows (native): found {len(rows)} of {max_n} requested "
                f"({len(eps)} visible rows)",
                step="13",
            )
            return win_y, win_h, rows
        # Native saw nothing (list not rendered?) — fall back to the slow walk.
        self.logger.log("_find_episode_rows: native saw no rows — System Events fallback",
                        step="13")
        return self._find_episode_rows_sysevents(max_n)

    def _find_episode_rows_sysevents(
        self, max_n: int
    ) -> tuple[int, int, dict[int, tuple[int, int, int, int, int, int]]]:
        """Fallback: one AppleScript/System Events BFS measuring episodes 1..max_n
        (~30s on the deep Catalyst tree). Kept as a safety net for _find_episode_rows.
        """
        script = f"""
        tell application "System Events"
            set frontmost of process "Podcasts" to true
        end tell
        delay 0.3
        tell application "System Events"
            tell process "Podcasts"
                if not (exists window 1) then return "ERROR:no_window"
                set maxN to {max_n}
                set seenCount to 0
                set outStr to ""
                set queue to {{window 1}}
                repeat 3000 times
                    if (count of queue) = 0 then exit repeat
                    set elem to item 1 of queue
                    if (count of queue) > 1 then
                        set queue to items 2 thru -1 of queue
                    else
                        set queue to {{}}
                    end if
                    set isBtn to false
                    try
                        if class of elem is button then set isBtn to true
                    end try
                    if isBtn then
                        set btnW to 0
                        set btnH to 0
                        try
                            set eSz to size of elem
                            set btnH to (item 2 of eSz) as integer
                            set btnW to (item 1 of eSz) as integer
                        end try
                        if btnH > 60 and btnW > 400 then
                            set seenCount to seenCount + 1
                            set ePos to position of elem
                            set eX to (item 1 of ePos) as integer
                            set eY to (item 2 of ePos) as integer
                            set moreX to 0
                            set moreY to 0
                            try
                                repeat with k in UI elements of elem
                                    set kd to ""
                                    try
                                        set kd to description of k as string
                                    end try
                                    if kd is "more" then
                                        set mp to position of k
                                        set ms to size of k
                                        set moreX to ((item 1 of mp) + (item 1 of ms) / 2) as integer
                                        set moreY to ((item 2 of mp) + (item 2 of ms) / 2) as integer
                                        exit repeat
                                    end if
                                    try
                                        repeat with gk in UI elements of k
                                            set gkd to ""
                                            try
                                                set gkd to description of gk as string
                                            end try
                                            if gkd is "more" then
                                                set mp to position of gk
                                                set ms to size of gk
                                                set moreX to ((item 1 of mp) + (item 1 of ms) / 2) as integer
                                                set moreY to ((item 2 of mp) + (item 2 of ms) / 2) as integer
                                                exit repeat
                                            end if
                                        end repeat
                                    end try
                                    if moreX > 0 then exit repeat
                                end repeat
                            end try
                            set outStr to outStr & "EP:" & seenCount & "," & eX & "," & eY & "," & btnW & "," & btnH & "," & moreX & "," & moreY & ";"
                            if seenCount = maxN then exit repeat
                        end if
                    end if
                    try
                        repeat with ch in UI elements of elem
                            set end of queue to ch
                        end repeat
                    end try
                end repeat
                set wPos to position of window 1
                set wSz to size of window 1
                set wY to (item 2 of wPos) as integer
                set wH to (item 2 of wSz) as integer
                return "WIN:" & wY & "," & wH & "|" & outStr
            end tell
        end tell
        """
        out = run_osascript(script, timeout=90, label=f"measure episodes 1..{max_n}")
        win_y = win_h = 0
        rows: dict[int, tuple[int, int, int, int, int, int]] = {}
        if out.startswith("ERROR:"):
            self.logger.log(f"_find_episode_rows: {out}", step="13")
            return win_y, win_h, rows
        head = out.split("|", 1)[0]
        if head.startswith("WIN:"):
            parts = head[4:].split(",")
            if len(parts) == 2:
                try:
                    win_y, win_h = int(parts[0]), int(parts[1])
                except ValueError:
                    pass
        # Episode entries are joined by ';' in the trailing segment after WIN:.
        if "|" in out:
            tail = out.split("|", 1)[1]
            for entry in tail.split(";"):
                if not entry.startswith("EP:"):
                    continue
                nums = entry[3:].split(",")
                if len(nums) != 7:
                    continue
                try:
                    n, eX, eY, eW, eH, mX, mY = (int(v) for v in nums)
                except ValueError:
                    continue
                rows[n] = (eX, eY, eW, eH, mX, mY)
        return win_y, win_h, rows

    def download_episode_rows(self, video_nos: list[int]) -> dict[int, str]:
        """Download several episodes of the current show with ONE BFS pass.

        Measures every requested row up-front (see _find_episode_rows), then
        pixel-clicks each download button.  Any episode not captured in the single
        pass (e.g. far enough down the list to need lazy-load scrolling) falls back
        to the per-episode download_episode_row, which still handles scrolling.
        """
        results: dict[int, str] = {}
        if not video_nos:
            return results
        max_n = max(video_nos)
        self.scroll_to_top()
        win_y, win_h, rows = self._find_episode_rows(max_n)
        for i, video_no in enumerate(video_nos):
            # Keep a 5–6s gap between consecutive download clicks. Firing them
            # back-to-back can make Podcasts drop/queue-fail the next download.
            if i > 0:
                self.logger.log(
                    f"Waiting {DOWNLOAD_GAP_SEC}s before episode {video_no} download",
                    step="13",
                )
                time.sleep(DOWNLOAD_GAP_SEC)
            rect = rows.get(video_no)
            if rect is None:
                self.logger.log(
                    f"Episode {video_no}: not in single-pass measurement — "
                    f"falling back to per-episode search",
                    step="13",
                )
                results[video_no] = self.download_episode_row(video_no)
                continue
            row_x, row_y, row_w, row_h, more_x, more_y = rect
            results[video_no] = self._click_download_at(
                win_y, win_h, row_x, row_y, row_w, row_h, more_x, more_y, video_no
            )
        return results

    def _click_download_at(
        self, win_y: int, win_h: int, row_x: int, row_y: int, row_w: int, row_h: int,
        more_x: int, more_y: int, video_no: int,
    ) -> str:
        """Hover the measured episode row and pixel-click its download icon."""
        # Make sure Podcasts is frontmost so it delivers hover/tracking events.
        try:
            run_osascript(
                'tell application "Podcasts" to activate',
                timeout=5, label="activate Podcasts before download click",
            )
            time.sleep(0.2)
        except AutomationError:
            pass

        try:
            import Quartz  # type: ignore[import]
        except ImportError:
            self.logger.log(
                "Quartz unavailable — cannot hover-click download button", step="13"
            )
            return "quartz_unavailable"

        def _mouse(kind, x, y):
            pt = Quartz.CGPointMake(x, y)
            ev = Quartz.CGEventCreateMouseEvent(
                None, kind, pt, Quartz.kCGMouseButtonLeft
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.05)

        # If the target row's click center is below the window viewport, scroll the
        # list down one page via AX so the row comes into view, then re-locate it by
        # scanning for the button closest to the estimated post-scroll y coordinate.
        # CGEventCreateScrollWheelEvent is ignored by Mac Catalyst, so we use
        # AXUIElementPerformAction("AXScrollDownByPage") on a visible episode button.
        # After the download click we undo the extra scroll (AXScrollUpByPage) so that
        # subsequent per-episode BFS searches start from the correct list position.
        _scrolled_into_view = False
        _ax_app2 = None
        if win_h > 0 and more_y >= win_y + win_h:
            try:
                from ApplicationServices import (  # type: ignore[import]
                    AXUIElementCreateApplication, AXUIElementCopyAttributeValue,
                    AXUIElementPerformAction, kAXChildrenAttribute, kAXRoleAttribute,
                    kAXPositionAttribute, kAXSizeAttribute, AXValueGetValue,
                    kAXValueCGPointType, kAXValueCGSizeType,
                )

                _ax_pid = int(subprocess.run(
                    ["osascript", "-e",
                     'tell application "System Events" to return unix id of process "Podcasts"'],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip())
                _ax_app2 = AXUIElementCreateApplication(_ax_pid)

                def _ep_btns(root):
                    stack = [root]; seen = 0
                    while stack and seen < 8000:
                        el = stack.pop(); seen += 1
                        err, role = AXUIElementCopyAttributeValue(el, kAXRoleAttribute, None)
                        if role == "AXButton":
                            err, pv = AXUIElementCopyAttributeValue(el, kAXPositionAttribute, None)
                            err, sv = AXUIElementCopyAttributeValue(el, kAXSizeAttribute, None)
                            if pv and sv:
                                _, pt = AXValueGetValue(pv, kAXValueCGPointType, None)
                                _, sz = AXValueGetValue(sv, kAXValueCGSizeType, None)
                                try:
                                    if int(sz.height) > 60 and int(sz.width) > 400:
                                        yield el, int(pt.y), int(pt.x), int(sz.width), int(sz.height)
                                except (OverflowError, ValueError):
                                    pass
                        err, ch = AXUIElementCopyAttributeValue(el, kAXChildrenAttribute, None)
                        if ch: stack.extend(ch)

                _btns = sorted(_ep_btns(_ax_app2), key=lambda r: r[1])
                _vis_el = next((el for el, ey, *_ in _btns if ey >= 100), None)
                if _vis_el is not None:
                    AXUIElementPerformAction(_vis_el, "AXScrollDownByPage")
                    time.sleep(0.5)
                    # Row moved ~602px up; find the button closest to estimated new y
                    _est_y = row_y - 600
                    _btns2 = list(_ep_btns(_ax_app2))
                    if _btns2:
                        _best = min(_btns2, key=lambda r: abs(r[1] - _est_y))
                        _, _by, _bx, _bw, _bh = _best
                        row_x, row_y, row_w, row_h = _bx, _by, _bw, _bh
                        more_x = row_x + row_w - 47
                        more_y = row_y + row_h // 2
                        self.logger.log(
                            f"Episode {video_no}: below-window scroll — "
                            f"row refreshed to ({row_x},{row_y},{row_w},{row_h})",
                            step="13",
                        )
                        _scrolled_into_view = True
            except Exception as _exc:
                self.logger.log(
                    f"Episode {video_no}: below-window scroll error: {_exc}", step="13"
                )
            if not _scrolled_into_view:
                # Last-resort clamp (click may miss — AX scroll failed)
                more_y = win_y + win_h - 10

        row_cx = row_x + row_w // 2
        row_cy = row_y + row_h // 2

        # Download icon is ~35 px left of the 'more' button center.
        # Fall back to 65 px from the row's right edge if more-button was not found.
        if more_x > 0:
            dl_x = more_x - 35
            dl_y = more_y
        else:
            dl_x = row_x + row_w - 65
            dl_y = row_cy

        self.logger.log(
            f"Episode {video_no}: row=({row_x},{row_y},{row_w},{row_h}) "
            f"more=({more_x},{more_y}) → hover ({row_cx},{row_cy}) "
            f"→ download click ({dl_x},{dl_y})",
            step="13",
        )

        # Hover over row center to trigger the hover state (shows download icon)
        _mouse(Quartz.kCGEventMouseMoved, row_cx, row_cy)
        time.sleep(0.2)

        # Move cursor to the download button position and wait for icons to render
        _mouse(Quartz.kCGEventMouseMoved, dl_x, dl_y)
        time.sleep(0.15)

        # Pre-click: AX scan to detect already-downloaded state (best-effort)
        hover_state = self._check_hover_downloaded(row_y, row_h, dl_x, dl_y)
        self.logger.log(
            f"Episode {video_no}: hover state check → {hover_state}", step="13"
        )
        if hover_state == "already_downloaded":
            _mouse(Quartz.kCGEventMouseMoved, row_cx, row_cy - 150)
            return "already_downloaded"

        # Click the download button
        _mouse(Quartz.kCGEventLeftMouseDown, dl_x, dl_y)
        time.sleep(0.1)
        _mouse(Quartz.kCGEventLeftMouseUp, dl_x, dl_y)
        time.sleep(0.3)

        # Safety net: if an unexpected delete/remove dialog appeared, cancel it
        dialog_result = self._dismiss_delete_dialog_if_unexpected()
        if dialog_result == "dismissed":
            self.logger.log(
                f"Episode {video_no}: delete dialog detected and dismissed — already downloaded",
                step="13",
            )
            return "already_downloaded_popup_dismissed"

        # Undo the extra below-window scroll so subsequent BFS searches count from
        # ep1, not from the shifted position.  One AXScrollUpByPage is the inverse of
        # the one AXScrollDownByPage we did above.
        if _scrolled_into_view and _ax_app2 is not None:
            try:
                _btns_r = sorted(_ep_btns(_ax_app2), key=lambda r: r[1])
                _restore_el = next((el for el, ey, *_ in _btns_r if ey >= 100), None)
                if _restore_el is not None:
                    AXUIElementPerformAction(_restore_el, "AXScrollUpByPage")
                    time.sleep(0.4)
                    self.logger.log(
                        f"Episode {video_no}: restored list scroll after below-window click",
                        step="13",
                    )
            except Exception:
                pass

        return "download_clicked"

    def _check_hover_downloaded(self, row_y: int, row_h: int, dl_x: int = 0, dl_y: int = 0) -> str:
        """Detect download button state after hover using direct AX position lookup.

        Uses AXUIElementCopyElementAtPosition to directly query the AX element at the
        download button coordinates — instant, no BFS traversal needed.
        Returns: 'already_downloaded' | 'ready_to_download' | 'no_download_available' | 'unknown'
        """
        if dl_x > 0 and dl_y > 0:
            try:
                from ApplicationServices import (  # type: ignore[import]
                    AXUIElementCreateApplication,
                    AXUIElementCopyElementAtPosition,
                    AXUIElementCopyAttributeValue,
                    kAXDescriptionAttribute,
                )
                # Get Podcasts PID (fast osascript call, ~5ms)
                pid_result = subprocess.run(
                    ["osascript", "-e",
                     "tell application \"System Events\"\nreturn unix id of process \"Podcasts\"\nend tell"],
                    capture_output=True, text=True, timeout=5,
                )
                pid = int(pid_result.stdout.strip())
                app_ref = AXUIElementCreateApplication(pid)
                err, elem = AXUIElementCopyElementAtPosition(app_ref, float(dl_x), float(dl_y), None)
                if err == 0 and elem is not None:
                    err2, desc = AXUIElementCopyAttributeValue(elem, kAXDescriptionAttribute, None)
                    desc_str = str(desc) if (err2 == 0 and desc) else ""
                    self.logger.log(
                        f"AX@({dl_x},{dl_y}) err={err} desc='{desc_str[:60]}'", step="13"
                    )
                    if "Remove Download" in desc_str:
                        return "already_downloaded"
                    if "Download" in desc_str:
                        return "ready_to_download"
                # Element found but description unrecognised — treat as unknown so
                # we still attempt the click (AXUIElementCopyElementAtPosition often
                # returns the underlying episode-row button rather than the small
                # hover-revealed download icon).
                return "unknown"
            except Exception:
                pass  # fall through to unknown
        return "unknown"

    def _ax_nodes(self, node_cap: int = 20000) -> list[tuple[str, str, int, int, int, int]]:
        """Native AX walk of the Podcasts app — one flat snapshot of every node.

        Returns a list of (role, text, x, y, w, h), where `text` is the first of
        AXDescription / AXValue / AXTitle that is a non-empty string.  Measured live:
        ~240 nodes in ~1s, and it DOES see the episode-list rows and Downloaded cards
        (once rendered) — unlike System Events traversal, which is the same data at
        20-35s.  All the See-All / episode / card finders are built on this.
        """
        try:
            from ApplicationServices import (  # type: ignore[import]
                AXUIElementCreateApplication,
                AXUIElementCopyAttributeValue,
                AXValueGetValue,
                kAXChildrenAttribute,
                kAXRoleAttribute,
                kAXDescriptionAttribute,
                kAXValueAttribute,
                kAXTitleAttribute,
                kAXPositionAttribute,
                kAXSizeAttribute,
                kAXValueCGPointType,
                kAXValueCGSizeType,
            )
        except Exception:
            return []
        try:
            pid_result = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to return unix id of process "Podcasts"'],
                capture_output=True, text=True, timeout=5,
            )
            pid = int(pid_result.stdout.strip())
        except Exception:
            return []

        app_ref = AXUIElementCreateApplication(pid)

        def _attr(el, a):
            try:
                err, val = AXUIElementCopyAttributeValue(el, a, None)
                return val if err == 0 else None
            except Exception:
                return None

        out: list[tuple[str, str, int, int, int, int]] = []
        stack = [app_ref]
        seen = 0
        while stack and seen < node_cap:
            el = stack.pop()
            seen += 1
            role = _attr(el, kAXRoleAttribute)
            text = ""
            for a in (kAXDescriptionAttribute, kAXValueAttribute, kAXTitleAttribute):
                v = _attr(el, a)
                if isinstance(v, str) and v:
                    text = v
                    break
            x = y = w = h = 0
            pv = _attr(el, kAXPositionAttribute)
            sv = _attr(el, kAXSizeAttribute)
            if pv is not None and sv is not None:
                okp, pt = AXValueGetValue(pv, kAXValueCGPointType, None)
                oks, sz = AXValueGetValue(sv, kAXValueCGSizeType, None)
                if okp and oks:
                    try:
                        x, y = int(pt.x), int(pt.y)
                        w, h = int(sz.width), int(sz.height)
                    except (OverflowError, ValueError):
                        x = y = w = h = 0
            out.append((str(role or ""), text, x, y, w, h))
            ch = _attr(el, kAXChildrenAttribute)
            if ch:
                stack.extend(ch)
        return out

    def _ax_find_text_center(
        self, needle: str, exclude: str | None = None, node_cap: int = 20000
    ) -> tuple[int, int] | None:
        """Find an element whose text contains `needle` via the native AX API.

        Walks the Podcasts AX tree with ApplicationServices (AXUIElement), reading
        AXDescription / AXValue / AXTitle on each node and returning the pixel center
        of the first match (excluding any whose text contains `exclude`).

        This replaces System Events traversal, which is unusably slow on the deeply
        nested Catalyst tree: iterating `entire contents` re-resolves an absolute
        reference per property read (~19s for 170 nodes), whereas this native walk
        covers ~240 nodes in ~0.6s (measured live).
        """
        try:
            from ApplicationServices import (  # type: ignore[import]
                AXUIElementCreateApplication,
                AXUIElementCopyAttributeValue,
                AXValueGetValue,
                kAXChildrenAttribute,
                kAXDescriptionAttribute,
                kAXValueAttribute,
                kAXTitleAttribute,
                kAXPositionAttribute,
                kAXSizeAttribute,
                kAXValueCGPointType,
                kAXValueCGSizeType,
            )
        except Exception:
            return None

        try:
            pid_result = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to return unix id of process "Podcasts"'],
                capture_output=True, text=True, timeout=5,
            )
            pid = int(pid_result.stdout.strip())
        except Exception:
            return None

        app_ref = AXUIElementCreateApplication(pid)

        def _attr(el, a):
            try:
                err, val = AXUIElementCopyAttributeValue(el, a, None)
                return val if err == 0 else None
            except Exception:
                return None

        stack = [app_ref]
        seen = 0
        text_attrs = (kAXDescriptionAttribute, kAXValueAttribute, kAXTitleAttribute)
        while stack and seen < node_cap:
            el = stack.pop()
            seen += 1
            for a in text_attrs:
                v = _attr(el, a)
                if isinstance(v, str) and needle in v and (
                    exclude is None or exclude not in v
                ):
                    pv = _attr(el, kAXPositionAttribute)
                    sv = _attr(el, kAXSizeAttribute)
                    if pv is not None and sv is not None:
                        okp, pt = AXValueGetValue(pv, kAXValueCGPointType, None)
                        oks, sz = AXValueGetValue(sv, kAXValueCGSizeType, None)
                        if okp and oks:
                            return (int(pt.x + sz.width / 2), int(pt.y + sz.height / 2))
                    break
            ch = _attr(el, kAXChildrenAttribute)
            if ch:
                stack.extend(ch)
        return None

    def _dismiss_delete_dialog_if_unexpected(self) -> str:
        """Check for an unexpected remove/delete sheet after a download click.

        If a removal confirmation sheet appeared (meaning we accidentally activated
        the delete icon instead of the download icon), press Escape to cancel it.
        Returns: 'dismissed' | 'no_dialog'
        """
        check_script = """
        tell application "System Events"
            tell process "Podcasts"
                set shCount to 0
                try
                    set shCount to count of sheets of window 1
                end try
                if shCount is 0 then return "no_dialog"
                set matchBtn to ""
                try
                    repeat with btn in buttons of sheet 1 of window 1
                        set bn to ""
                        try
                            set bn to name of btn as string
                        end try
                        if bn contains "Remove" or bn contains "Delete" then
                            set matchBtn to bn
                            exit repeat
                        end if
                    end repeat
                end try
                if matchBtn is not "" then return "delete_sheet:" & matchBtn
                return "sheet_unknown"
            end tell
        end tell
        """
        try:
            result = run_osascript(check_script, timeout=5, label="check for unexpected delete dialog")
            result = result.strip()
            if result == "no_dialog":
                return "no_dialog"
            # A sheet appeared — dismiss it with Escape
            try:
                import Quartz as _Q
                for _down in (True, False):
                    ev = _Q.CGEventCreateKeyboardEvent(None, 0x35, _down)
                    _Q.CGEventPost(_Q.kCGHIDEventTap, ev)
                    time.sleep(0.05)
            except ImportError:
                try:
                    run_osascript(
                        'tell application "System Events" to key code 53',
                        timeout=5, label="Escape to dismiss delete dialog",
                    )
                except AutomationError:
                    pass
            time.sleep(0.5)
            self.logger.log(f"Unexpected delete dialog dismissed: {result}", step="13")
            return "dismissed"
        except AutomationError:
            return "no_dialog"

    def cleanup_episode_row(self, video_no: int) -> str:
        """Remove a download via the episode-list ⋯ menu (Down×1+Enter = Remove Download).

        Uses the same BFS as download_episode_row to locate episode N's more-button,
        then clicks it and navigates to 'Remove Download'.  This keeps the show in the
        library (unlike the Downloaded-tab show-card approach which triggers
        'Remove From Library'), making the card consistently visible on the next cycle.
        """
        script = f"""
        tell application "System Events"
            set frontmost of process "Podcasts" to true
        end tell
        delay 0.3
        tell application "System Events"
            tell process "Podcasts"
                if not (exists window 1) then return "ERROR:no_window"
                set targetN to {video_no}
                set seenCount to 0
                set targetEp to missing value
                set queue to {{window 1}}
                set deadline to (current date) + 75
                repeat 3000 times
                    if (count of queue) = 0 then exit repeat
                    if (current date) > deadline then return "ERROR:deadline_exceeded"
                    set elem to item 1 of queue
                    if (count of queue) > 1 then
                        set queue to items 2 thru -1 of queue
                    else
                        set queue to {{}}
                    end if
                    set isBtn to false
                    try
                        if class of elem is button then set isBtn to true
                    end try
                    if isBtn then
                        set looksLikeEpisode to false
                        try
                            set eSz to size of elem
                            set btnH to (item 2 of eSz) as integer
                            set btnW to (item 1 of eSz) as integer
                            if btnH > 60 and btnW > 400 then
                                set looksLikeEpisode to true
                            end if
                        end try
                        if looksLikeEpisode then
                            set seenCount to seenCount + 1
                            if seenCount = targetN then
                                set targetEp to elem
                                exit repeat
                            end if
                        end if
                    end if
                    try
                        repeat with ch in UI elements of elem
                            set end of queue to ch
                        end repeat
                    end try
                end repeat
                if targetEp is missing value then
                    return "ERROR:episode_not_found|seen=" & seenCount
                end if
                set ePos to position of targetEp
                set eSz to size of targetEp
                set eX to (item 1 of ePos) as integer
                set eY to (item 2 of ePos) as integer
                set eW to (item 1 of eSz) as integer
                set eH to (item 2 of eSz) as integer
                set moreX to 0
                set moreY to 0
                try
                    repeat with k in UI elements of targetEp
                        set kd to ""
                        try
                            set kd to description of k as string
                        end try
                        if kd is "more" then
                            set mp to position of k
                            set ms to size of k
                            set moreX to ((item 1 of mp) + (item 1 of ms) / 2) as integer
                            set moreY to ((item 2 of mp) + (item 2 of ms) / 2) as integer
                            exit repeat
                        end if
                        try
                            repeat with gk in UI elements of k
                                set gkd to ""
                                try
                                    set gkd to description of gk as string
                                end try
                                if gkd is "more" then
                                    set mp to position of gk
                                    set ms to size of gk
                                    set moreX to ((item 1 of mp) + (item 1 of ms) / 2) as integer
                                    set moreY to ((item 2 of mp) + (item 2 of ms) / 2) as integer
                                    exit repeat
                                end if
                            end repeat
                        end try
                        if moreX > 0 then exit repeat
                    end repeat
                end try
                return "ROW:" & eX & "," & eY & "," & eW & "," & eH & "|MORE:" & moreX & "," & moreY
            end tell
        end tell
        """
        out = run_osascript(script, timeout=90, label=f"find episode {video_no} for cleanup")
        if out.startswith("ERROR:"):
            self.logger.log(f"Cleanup episode {video_no}: {out}", step="14")
            return "episode_not_found"

        row_x = row_y = row_w = row_h = more_x = more_y = 0
        for chunk in out.split("|"):
            if chunk.startswith("ROW:"):
                parts = chunk[4:].split(",")
                if len(parts) == 4:
                    try:
                        row_x, row_y, row_w, row_h = (
                            int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
                        )
                    except ValueError:
                        pass
            elif chunk.startswith("MORE:"):
                parts = chunk[5:].split(",")
                if len(parts) == 2:
                    try:
                        more_x, more_y = int(parts[0]), int(parts[1])
                    except ValueError:
                        pass

        if row_w == 0 or more_x == 0:
            self.logger.log(f"Cleanup episode {video_no}: bad position '{out}'", step="14")
            return "episode_not_found"

        try:
            import Quartz  # type: ignore[import]
        except ImportError:
            return "quartz_unavailable"

        def _mouse(kind, x, y):
            pt = Quartz.CGPointMake(x, y)
            ev = Quartz.CGEventCreateMouseEvent(None, kind, pt, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.05)

        def _key(vk, down):
            ev = Quartz.CGEventCreateKeyboardEvent(None, vk, down)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.05)

        row_cx = row_x + row_w // 2
        row_cy = row_y + row_h // 2

        self.logger.log(
            f"Cleanup episode {video_no}: row=({row_x},{row_y},{row_w},{row_h}) "
            f"more=({more_x},{more_y}) → hover ({row_cx},{row_cy}) → ⋯ click ({more_x},{more_y})",
            step="14",
        )

        # Hover at row center to trigger hover state, then move to ⋯ and click
        _mouse(Quartz.kCGEventMouseMoved, row_cx, row_cy)
        time.sleep(0.3)
        _mouse(Quartz.kCGEventMouseMoved, more_x, more_y)
        time.sleep(0.2)
        _mouse(Quartz.kCGEventLeftMouseDown, more_x, more_y)
        time.sleep(0.1)
        _mouse(Quartz.kCGEventLeftMouseUp, more_x, more_y)
        time.sleep(0.8)

        ss = self._take_screenshot("cleanup_context_menu")
        self.logger.log(f"⋯ clicked, screenshot: {ss}", step="14")

        # Down×1+Enter → "Remove Download" (first item in episode ⋯ menu)
        _key(0x7D, True); _key(0x7D, False)
        time.sleep(0.2)
        _key(0x24, True); _key(0x24, False)
        time.sleep(0.8)

        ss = self._take_screenshot("cleanup_after_enter")
        self.logger.log(f"Down×1+Enter done, screenshot: {ss}", step="14")

        # Check for confirmation dialog (may appear for non-followed shows)
        remove = self._click_confirmation_remove()
        self.logger.log(f"Cleanup confirmation: {remove}", step="14")

        if "clicked" in remove or remove == "no_sheet":
            return "removed"
        return f"remove_failed:{remove}"

    def _take_screenshot(self, label: str) -> str:
        """Capture a timestamped screenshot to the logs/ss/ directory."""
        ss_dir = self.logger.log_path.parent / "ss"
        ss_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H%M%S")
        path = str(ss_dir / f"{stamp}_{label}.png")
        subprocess.run(["screencapture", "-x", path], capture_output=True)
        return path

    def navigate_to_downloaded_tab(self) -> str:
        """Navigate to the Downloaded section in the Podcasts sidebar.

        Uses the fast ApplicationServices AX walk (_ax_nodes) to find the
        'Downloaded' sidebar item (AXStaticText, x<400, w>100) and Quartz-click it.
        """
        try:
            import Quartz  # type: ignore[import]
        except ImportError:
            return "quartz_unavailable"

        try:
            run_osascript('tell application "Podcasts" to activate',
                          timeout=5, label="activate before Downloaded nav")
        except AutomationError:
            pass
        time.sleep(0.3)

        nodes = self._ax_nodes()
        cx = cy = 0
        for role, t, x, y, w, h in nodes:
            if t == "Downloaded" and x < 400 and w > 100 and h > 0:
                cx = x + w // 2
                cy = y + h // 2
                break

        if not cx:
            self.logger.log("navigate_to_downloaded_tab: ERROR:not_found", step="14")
            return "not_found"

        def _mouse(kind, px, py):
            pt = Quartz.CGPointMake(px, py)
            ev = Quartz.CGEventCreateMouseEvent(None, kind, pt, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.05)

        _mouse(Quartz.kCGEventMouseMoved, cx, cy)
        time.sleep(0.15)
        _mouse(Quartz.kCGEventLeftMouseDown, cx, cy)
        time.sleep(0.1)
        _mouse(Quartz.kCGEventLeftMouseUp, cx, cy)
        time.sleep(0.4)

        self.logger.log(f"Clicked Downloaded sidebar at ({cx},{cy})", step="14")
        return "navigated"

    def show_downloading_page(self, wait_timeout: int = 1800) -> str:
        """Open the 'Downloading' progress modal, then wait for it to auto-close.

        Run right after every show's episodes have been queued.  Apple Podcasts
        surfaces a 'Downloading' entry at the top of the Downloaded view while
        downloads are active; clicking it opens the 'Downloads' modal (Cancel All /
        Done + per-episode progress).  Podcasts auto-dismisses that modal once every
        queued episode has finished, so its disappearance is the all-downloads-complete
        signal — we open it, then do nothing but poll until it closes, at which point
        cleanup can start immediately with no further waiting.

        Returns:
          'completed'          – modal opened and then auto-closed → downloads done.
          'no_downloading_item'– nothing queued/already finished → treat as done.
          'opened_timeout'     – modal opened but did not close within wait_timeout.
          'clicked_unconfirmed'– clicked 'Downloading' but the modal was not detected.
          'not_navigated' | 'click_failed' – could not get there.
        """
        self.activate()
        self.wait_for_window()
        nav = self.navigate_to_downloaded_tab()
        self.logger.log(f"Downloading page: navigated to Downloaded ({nav})", step="13")
        if nav != "navigated":
            return "not_navigated"

        # The 'Downloading' entry can take a moment to register after navigation
        # (the download has to be accepted into the queue first). Probe several
        # times over ~15s before giving up.
        pos: tuple[int, int] | None = None
        for delay in (2, 3, 4):
            time.sleep(delay)
            pos = self._find_downloading_button()
            if pos is not None:
                break
        if pos is None:
            # Dump the AX tree so we can see what the Downloaded page actually
            # exposes (the 'Downloading' element may use a label we don't match yet,
            # or the queue may already be empty because the episodes finished).
            dump = self._dump_ax_tree(
                "downloading_page_not_found", max_depth=12, max_elements=1500
            )
            self.logger.log(
                f"Downloading page: no 'Downloading' entry found — AX tree dumped to "
                f"{dump} (downloads may have finished already)",
                step="13",
            )
            return "no_downloading_item"

        try:
            import Quartz  # type: ignore[import]
        except ImportError:
            return "click_failed"

        cx, cy = pos

        def _mouse(kind, x, y):
            pt = Quartz.CGPointMake(x, y)
            ev = Quartz.CGEventCreateMouseEvent(None, kind, pt, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.05)

        # Click the 'Downloading' header; clicking it opens the 'Downloads' modal
        # (the sheet with Cancel All / Done and per-episode progress bars).  Verify
        # the modal actually appeared and retry once if it didn't — the header sits
        # close to the toolbar, so a single click can occasionally miss.
        opened = False
        for attempt in range(2):
            try:
                _mouse(Quartz.kCGEventMouseMoved, cx, cy)
                time.sleep(0.2)
                _mouse(Quartz.kCGEventLeftMouseDown, cx, cy)
                time.sleep(0.1)
                _mouse(Quartz.kCGEventLeftMouseUp, cx, cy)
                time.sleep(0.6)
            except Exception as exc:
                self.logger.log(f"Downloading page: click failed ({exc})", step="13")
                return "click_failed"

            if self._downloads_modal_open():
                self.logger.log(
                    f"Downloading page: opened Downloads modal at ({cx},{cy}) "
                    f"(attempt {attempt + 1})",
                    step="13",
                )
                opened = True
                break
            # Re-locate the header before retrying (layout may have shifted).
            repos = self._find_downloading_button()
            if repos is not None:
                cx, cy = repos

        if not opened:
            self.logger.log(
                f"Downloading page: clicked 'Downloading' at ({cx},{cy}) but the "
                f"Downloads modal was not detected",
                step="13",
            )
            return "clicked_unconfirmed"

        # Now do nothing but wait for the modal to auto-close. Podcasts dismisses it
        # the moment the last queued episode finishes downloading, so its
        # disappearance is the completion signal — as soon as it's gone we return and
        # cleanup starts immediately, with no extra fixed wait.
        t_open = time.time()
        deadline = t_open + wait_timeout
        while time.time() < deadline:
            time.sleep(3)
            if not self._downloads_modal_open():
                waited = int(time.time() - t_open)
                self.logger.log(
                    f"Downloading page: modal auto-closed after {waited}s — all "
                    f"downloads complete; starting cleanup",
                    step="13",
                )
                return "completed"
            waited = int(time.time() - t_open)
            self.logger.log(f"Downloads still in progress ({waited}s)", step="13")

        self.logger.log(
            f"Downloading page: modal still open after {wait_timeout}s — proceeding",
            step="13",
        )
        return "opened_timeout"

    def _downloads_modal_open(self) -> bool:
        """True if the 'Downloads' progress modal (Cancel All / Done) is showing.

        Detected by the presence of the 'Cancel All' control via the native AX walk
        (whole app tree, so it finds the control whether the modal is a sheet or a
        child window).
        """
        return self._ax_find_text_center("Cancel All") is not None

    def _dump_ax_tree(self, label: str, max_depth: int = 6, max_elements: int = 500) -> str:
        """Dump the Podcasts AX tree to a text file in logs/.

        Called automatically whenever an AX selector returns no result.
        Format per line: role | title | description | value_snippet | frame | children_count
        Returns the dump file path (or '' on failure).
        """
        script = f"""
        tell application "System Events"
            tell process "Podcasts"
                if not (exists window 1) then return "ERROR:no_window"
                set output to ""
                set q to {{{{window 1, 0}}}}
                set elemCount to 0
                repeat {max_elements + 10} times
                    if (count of q) = 0 then exit repeat
                    set item_ to item 1 of q
                    if (count of q) > 1 then
                        set q to items 2 thru -1 of q
                    else
                        set q to {{}}
                    end if
                    set elem to item 1 of item_
                    set depth to item 2 of item_
                    if depth > {max_depth} then
                    else if elemCount < {max_elements} then
                        set elemCount to elemCount + 1
                        set eRole to "" & depth & " "
                        set eTitle to ""
                        set eDesc to ""
                        set eVal to ""
                        set eFrame to ""
                        set eCnt to 0
                        try
                            set eRole to eRole & (role of elem as string)
                        end try
                        try
                            set eTitle to title of elem as string
                        end try
                        try
                            set eDesc to description of elem as string
                        end try
                        try
                            set v to value of elem as string
                            if (length of v) > 80 then set v to (text 1 thru 80 of v) & "…"
                            set eVal to v
                        end try
                        try
                            set ePos to position of elem
                            set eSz to size of elem
                            set eFrame to (item 1 of ePos as integer) & "," & (item 2 of ePos as integer) & "," & (item 1 of eSz as integer) & "," & (item 2 of eSz as integer)
                        end try
                        try
                            set eCnt to count of UI elements of elem
                        end try
                        set output to output & eRole & "|" & eTitle & "|" & eDesc & "|" & eVal & "|" & eFrame & "|" & eCnt & linefeed
                        try
                            repeat with ch in UI elements of elem
                                set end of q to {{ch, depth + 1}}
                            end repeat
                        end try
                    end if
                end repeat
                return output
            end tell
        end tell
        """
        dump_dir = self.logger.log_path.parent
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_label = label.replace("/", "_").replace(" ", "_")[:60]
        dump_path = dump_dir / f"ax-dump-{safe_label}-{stamp}.txt"
        try:
            raw = run_osascript(script, timeout=30, label=f"ax_dump_{safe_label}")
            header = (
                f"# AX dump: {label}\n"
                f"# Generated: {datetime.now().astimezone().isoformat()}\n"
                f"# Format: depth+role | title | description | value | frame(x,y,w,h) | children\n\n"
            )
            dump_path.write_text(header + raw, encoding="utf-8")
            self.logger.log(f"AX dump saved: {dump_path}", step="AX", label=label)
            return str(dump_path)
        except Exception as exc:
            self.logger.log(f"AX dump failed ({label}): {exc}", step="AX")
            return ""

    def _has_back_button(self) -> bool:
        """Return True if a 'Back' nav button is visible — indicates we're on a show
        detail page inside the Downloads section, NOT on the top-level Downloads grid."""
        nodes = self._ax_nodes()
        win_y = 0
        for role, _t, x, y, w, h in nodes:
            if role == "AXWindow" and w > 400 and h > 400:
                win_y = y
                break
        for role, text, x, y, w, h in nodes:
            if role == "AXButton" and "Back" in text and w < 60 and y < win_y + 140:
                return True
        return False

    def _click_back_button(self) -> None:
        """Click the Back navigation button to return to the Downloads grid."""
        nodes = self._ax_nodes()
        win_y = 0
        for role, _t, x, y, w, h in nodes:
            if role == "AXWindow" and w > 400 and h > 400:
                win_y = y
                break
        for role, text, x, y, w, h in nodes:
            if role == "AXButton" and "Back" in text and w < 60 and y < win_y + 140:
                cx, cy = x + w // 2, y + h // 2
                try:
                    import Quartz as _Q
                    pt = _Q.CGPointMake(cx, cy)
                    for kind in (_Q.kCGEventMouseMoved, _Q.kCGEventLeftMouseDown, _Q.kCGEventLeftMouseUp):
                        ev = _Q.CGEventCreateMouseEvent(None, kind, pt, _Q.kCGMouseButtonLeft)
                        _Q.CGEventPost(_Q.kCGHIDEventTap, ev)
                        import time as _t; _t.sleep(0.05)
                    self.logger.log(f"Clicked Back button at ({cx},{cy})", step="14")
                except Exception as exc:
                    self.logger.log(f"Back button click failed: {exc}", step="14")
                return

    def _find_downloaded_card_frame(self) -> tuple[int, int, int, int] | None:
        """Find the first show card on the Downloaded tab via the native AX walk (~1s).

        Returns (x, y, w, h). Card criteria match the old System Events version: in
        the content area (right of the sidebar), roughly square-ish, 80–800 px per
        side, not full-width.  Picks the top-left-most card.  Falls back to the
        System Events walk (~30s) if the native walk finds none.
        """
        nodes = self._ax_nodes()
        win_x = win_y = win_w = win_h = 0
        for role, _t, x, y, w, h in nodes:
            if role == "AXWindow" and w > 400 and h > 400:
                win_x, win_y, win_w, win_h = x, y, w, h
                break
        if win_w == 0:
            return self._find_downloaded_card_frame_sysevents()
        content_left = win_x + 240  # conservative: sidebar can be wider than 180px on some displays
        content_top = win_y + 60    # below control bar; rejects nav-bar artwork near window top
        win_right = win_x + win_w
        win_bottom = win_y + win_h
        cards = [
            (x, y, w, h) for role, _t, x, y, w, h in nodes
            if x > content_left and y > content_top
            and x < win_right and y < win_bottom   # must be inside the actual window
            and 80 <= w <= 800 and 80 <= h <= 900
            and h > w               # Downloads grid cards are portrait (taller than wide)
            and h < w * 4           # but not an impossibly thin strip
            and w < win_w - 100
        ]
        if cards:
            cards.sort(key=lambda c: (c[1], c[0]))  # top-left-most first
            cx, cy, cw, ch = cards[0]
            self.logger.log(
                f"Downloaded card (native): ({cx},{cy},{cw},{ch})", step="14")
            return cx, cy, cw, ch
        # Native saw no card — could be genuinely empty, or a render lag. Let the
        # System Events walk confirm (it's slow but authoritative).
        return self._find_downloaded_card_frame_sysevents()

    def _find_downloaded_card_frame_sysevents(self) -> tuple[int, int, int, int] | None:
        """Fallback: System Events BFS for the first Downloaded card (~30s). Kept as a
        safety net for _find_downloaded_card_frame.
        """
        script = """
        tell application "System Events"
            tell process "Podcasts"
                if not (exists window 1) then return "ERROR:no_window"
                set wPos to position of window 1
                set wSz to size of window 1
                set wX to (item 1 of wPos) as integer
                set wY to (item 2 of wPos) as integer
                set wW to (item 1 of wSz) as integer
                set wH to (item 2 of wSz) as integer
                -- Content area begins past the sidebar (~180px from window left)
                set contentLeft to wX + 180
                set q to {window 1}
                repeat 3000 times
                    if (count of q) = 0 then exit repeat
                    set elem to item 1 of q
                    if (count of q) > 1 then
                        set q to items 2 thru -1 of q
                    else
                        set q to {}
                    end if
                    set eX to 0
                    set eY to 0
                    set eW to 0
                    set eH to 0
                    try
                        set ePos to position of elem
                        set eSz to size of elem
                        set eX to (item 1 of ePos) as integer
                        set eY to (item 2 of ePos) as integer
                        set eW to (item 1 of eSz) as integer
                        set eH to (item 2 of eSz) as integer
                    end try
                    -- Card criteria: in content area, portrait orientation (h > w),
                    -- below the control bar (y > wY+60), and within window bounds.
                    -- Phantom AX elements can have x/y coordinates far off-screen (e.g. 21523).
                    if eX > contentLeft and eX < wX + wW and eY > wY + 60 and eY < wY + wH and eW >= 80 and eH >= 80 and eW <= 800 and eH <= 900 then
                        -- Portrait: card must be taller than wide (excludes landscape nav-bar elements)
                        if eH > eW then
                            -- Exclude elements that span the full window width (containers, scroll areas)
                            if eW < wW - 100 then
                                return "CARD:" & eX & "," & eY & "," & eW & "," & eH & "|WIN:" & wX & "," & wY & "," & wW & "," & wH
                            end if
                        end if
                    end if
                    try
                        repeat with ch in UI elements of elem
                            set end of q to ch
                        end repeat
                    end try
                end repeat
                return "NOCARD|WIN:" & wX & "," & wY & "," & wW & "," & wH
            end tell
        end tell
        """
        out = run_osascript(script, timeout=90, label="find downloaded card frame")
        if out.startswith("ERROR:"):
            self.logger.log(f"_find_downloaded_card_frame: {out}", step="14")
            return None

        win_x = win_y = win_w = win_h = 0
        card_x = card_y = card_w = card_h = 0
        found_card = False

        for chunk in out.split("|"):
            if chunk.startswith("CARD:"):
                nums = chunk[5:].split(",")
                if len(nums) == 4:
                    try:
                        card_x, card_y, card_w, card_h = (
                            int(nums[0]), int(nums[1]), int(nums[2]), int(nums[3])
                        )
                        found_card = True
                    except ValueError:
                        pass
            elif chunk.startswith("WIN:"):
                nums = chunk[4:].split(",")
                if len(nums) >= 4:
                    try:
                        win_x, win_y, win_w, win_h = (
                            int(nums[0]), int(nums[1]), int(nums[2]), int(nums[3])
                        )
                    except ValueError:
                        pass
                elif len(nums) >= 2:
                    try:
                        win_x, win_y = int(nums[0]), int(nums[1])
                    except ValueError:
                        pass

        if found_card:
            self.logger.log(
                f"Downloaded card found via AX: ({card_x},{card_y},{card_w},{card_h})",
                step="14",
            )
            return card_x, card_y, card_w, card_h

        self.logger.log(
            f"Card not found via AX (win={win_x},{win_y},{win_w},{win_h}) — no fallback",
            step="14",
        )
        return None

    def _find_downloading_button(self) -> tuple[int, int] | None:
        """Find the 'Downloading' element in the Downloaded view; return its center.

        The Downloaded view shows a 'Downloading' header at the top while downloads
        are queued; clicking it opens the in-progress Downloads modal.  Returns
        (cx, cy) pixel center if found, else None.  Uses the native AX walk
        (~0.6s) — `exclude="Downloaded"` so the sidebar item never matches.
        """
        return self._ax_find_text_center("Downloading", exclude="Downloaded")

    def wait_for_downloads_stable(self, timeout: int = 180) -> str:
        """Wait for all downloads to finish by monitoring Podcasts' Downloading indicator.

        Strategy:
          1. Navigate to the Downloaded tab where the 'Downloading' progress section
             appears at the top of the page when downloads are active.
          2. Probe up to 10s (5 × 2s) for a 'Downloading' element to appear.
          3. If found: click it (opens the progress view) then poll every 3s until
             the element disappears — Podcasts auto-closes the view when all done.
          4. After the indicator clears (or was never seen): wait 5s then proceed.
          5. Returns 'completed' | 'completed_fast' | 'timeout'.
        """
        t_start = time.time()
        pos: tuple[int, int] | None = None

        # If show_downloading_page() left the 'Downloads' progress modal open, it is a
        # blocking sheet that would swallow the sidebar navigation below. Dismiss it
        # first with Escape (downloads keep running in the background — we never touch
        # 'Cancel All').
        if self._downloads_modal_open():
            run_osascript(
                'tell application "System Events" to key code 53',  # Escape
                timeout=5, label="dismiss Downloads modal before cleanup",
            )
            time.sleep(0.5)

        # Navigate to the Downloaded tab — the 'Downloading' section is at the top.
        nav = self.navigate_to_downloaded_tab()
        self.logger.log(f"Download wait: navigated to Downloaded tab ({nav})", step="14")

        # Two-pass probe: check at ~3s then ~11s after nav.
        # If the Downloading button appears, it means episodes are still in flight.
        # If it never appears, downloads finished before/during nav (fast connection).
        for attempt, delay in enumerate((2, 5)):
            time.sleep(delay)
            pos = self._find_downloading_button()
            if pos is not None:
                break
            elapsed = int(time.time() - t_start)
            self.logger.log(
                f"Download queue check {attempt + 1}/2: not visible yet ({elapsed}s)",
                step="14",
            )

        if pos is None:
            # Downloading indicator never appeared — downloads finished quickly.
            self.logger.log(
                "Downloading button not found — downloads likely done",
                step="14",
            )
            time.sleep(2)
            elapsed = int(time.time() - t_start)
            self.state.data.update({
                "download_state": "completed_fast",
                "download_wait_seconds": elapsed,
                "can_cleanup": True,
            })
            self.state.save()
            return "completed_fast"

        # Found the Downloading button — click it to open the progress view.
        try:
            import Quartz  # type: ignore[import]
            cx, cy = pos

            def _mouse(kind, x, y):
                pt = Quartz.CGPointMake(x, y)
                ev = Quartz.CGEventCreateMouseEvent(None, kind, pt, Quartz.kCGMouseButtonLeft)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                time.sleep(0.05)

            _mouse(Quartz.kCGEventMouseMoved, cx, cy)
            time.sleep(0.2)
            _mouse(Quartz.kCGEventLeftMouseDown, cx, cy)
            time.sleep(0.1)
            _mouse(Quartz.kCGEventLeftMouseUp, cx, cy)
            time.sleep(0.5)
            self.logger.log(f"Clicked Downloading progress button at ({cx},{cy})", step="14")
        except Exception as exc:
            self.logger.log(f"Downloading button click failed: {exc}", step="14")

        # Poll until the Downloading indicator disappears (all done) or timeout.
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(2)
            elapsed = int(time.time() - t_start)
            still_active = self._find_downloading_button() is not None
            self.logger.log(f"Downloads in progress ({elapsed}s)", step="14")
            if not still_active:
                time.sleep(2)
                elapsed = int(time.time() - t_start)
                self.logger.log(
                    f"Downloads complete after {elapsed}s",
                    step="14", download_state="completed", download_wait_seconds=elapsed,
                )
                self.state.data.update({
                    "download_state": "completed",
                    "download_wait_seconds": elapsed,
                    "can_cleanup": True,
                })
                self.state.save()
                return "completed"

        elapsed = int(time.time() - t_start)
        self.logger.log(
            f"Download wait timed out after {elapsed}s — proceeding anyway",
            step="14", download_state="timeout", download_wait_seconds=elapsed,
        )
        time.sleep(2)
        self.state.data.update({
            "download_state": "timeout",
            "download_wait_seconds": elapsed,
            "can_cleanup": True,
        })
        self.state.save()
        return "timeout"

    def wait_for_download_complete(self, timeout: int = 180) -> str:
        """Compatibility shim — delegates to wait_for_downloads_stable."""
        return self.wait_for_downloads_stable(timeout=timeout)

    def _click_downloaded_card_three_dots(self) -> str:
        """Hover over the show card in the Downloaded view and click its ⋯ button.

        Finds the card via AX BFS; falls back to a window-relative calculation.
        The ⋯ button is hover-only, so its position is computed from the card's AXFrame
        (near the bottom-right corner) rather than hardcoded window offsets.
        """
        frame = self._find_downloaded_card_frame()
        if frame is None:
            return "no_card_found"

        card_x, card_y, card_w, card_h = frame
        card_cx = card_x + card_w // 2
        card_cy = card_y + card_h // 2
        # ⋯ appears near the bottom-right of the card on hover
        three_dots_x = card_x + card_w - 30
        three_dots_y = card_y + card_h - 25

        try:
            import Quartz  # type: ignore[import]
        except ImportError:
            return "quartz_unavailable"

        # Bring Podcasts to front before Quartz events so they land on the right window.
        try:
            run_osascript(
                'tell application "Podcasts" to activate',
                timeout=5, label="activate Podcasts before three-dots click",
            )
            time.sleep(0.4)
        except AutomationError:
            pass

        def _mouse(kind, x, y):
            pt = Quartz.CGPointMake(x, y)
            ev = Quartz.CGEventCreateMouseEvent(None, kind, pt, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.05)

        self.logger.log(
            f"Card hover ({card_cx},{card_cy}) → ⋯ click ({three_dots_x},{three_dots_y})",
            step="14",
        )

        _mouse(Quartz.kCGEventMouseMoved, card_cx, card_cy)
        time.sleep(0.4)
        _mouse(Quartz.kCGEventMouseMoved, three_dots_x, three_dots_y)
        time.sleep(0.2)
        _mouse(Quartz.kCGEventLeftMouseDown, three_dots_x, three_dots_y)
        time.sleep(0.1)
        _mouse(Quartz.kCGEventLeftMouseUp, three_dots_x, three_dots_y)
        time.sleep(0.8)

        return "three_dots_clicked"

    def _click_confirmation_remove(self, max_attempts: int = 20) -> str:
        """Click the destructive Remove button in the confirmation sheet.

        After the context menu's Remove item is activated, Podcasts shows a native
        macOS sheet (accessible via AX) with a Remove From Library button.

        max_attempts: how many 0.4s polls to run (default 20 = 8s).  Pass a smaller
        value (e.g. 3) for a quick 1.2s probe when retrying keyboard nav.
        """
        script = """
        tell application "System Events"
            tell process "Podcasts"
                set shCount to 0
                try
                    set shCount to count of sheets of window 1
                end try
                if shCount is 0 then return "no_sheet"
                repeat with btn in buttons of sheet 1 of window 1
                    set bn to ""
                    try
                        set bn to name of btn as string
                    end try
                    if bn contains "Remove" or bn contains "Delete" then
                        click btn
                        delay 0.5
                        return "clicked:" & bn
                    end if
                end repeat
                return "no_remove_button"
            end tell
        end tell
        """
        # The sheet usually becomes AX-accessible within a couple of seconds.  Poll
        # quickly (0.4s) so we react the instant it appears, and cap the wait at ~8s:
        # the old 20×1.5s loop burned a flat 30s on every removal that produced no
        # confirmation sheet at all, which dominated per-show removal time.
        out = "no_sheet"
        for _attempt in range(max_attempts):
            out = run_osascript(script, timeout=5, label="click Remove in confirmation sheet")
            if out != "no_sheet":
                break
            time.sleep(0.4)
        self.logger.log(f"Confirmation sheet click: {out}", step="14")
        return out

    def open_downloaded_sidebar(self) -> str:
        """Legacy method — superseded by navigate_to_downloaded_tab."""
        return self.navigate_to_downloaded_tab()

    def check_downloads_state(self) -> dict[str, Any]:
        # Use entire contents to recursively search all nested elements.
        script = """
        tell application "System Events"
            tell process "Podcasts"
                if not (exists window 1) then return "no_window<<|>>0<<|>>"
                set downloadingCount to 0
                try
                    set allTexts to every static text of entire contents of window 1
                    repeat with s in allTexts
                        set t to ""
                        try
                            set t to (value of s as text)
                        end try
                        if t contains "Downloading" then
                            set downloadingCount to downloadingCount + 1
                        end if
                    end repeat
                end try
                if downloadingCount > 0 then
                    return "downloads_in_progress<<|>>" & downloadingCount & "<<|>>" & ""
                end if
                return "download_status_unknown<<|>>0<<|>>" & ""
            end tell
        end tell
        """
        out = run_osascript(script, timeout=25, label="check downloads")
        status, count_raw, sample = (out.split("<<|>>", 2) + ["", ""])[:3]
        try:
            count = int(count_raw)
        except ValueError:
            count = 0
        return {"status": status, "count": count, "sample": sample}

    # ── Show-info-targeted cleanup (primary — uses episode list, not Downloaded tab) ──

    def cleanup_by_show_info(self, show_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove downloaded episodes by navigating to each show's episode list.

        Uses the episode row ⋯ menu which IS AX-accessible, unlike the
        Downloaded tab's show cards (Mac Catalyst does not expose card text via AX).
        """
        results: list[dict[str, Any]] = []
        for entry in show_entries:
            show_name = entry.get("show_name", "unknown")
            show_url = entry.get("url", "")
            videos_downloaded: list[int] = entry.get("videos_downloaded") or entry.get("videos_requested") or []
            result = self._cleanup_show_via_episode_list(show_url, show_name, videos_downloaded)
            results.append({"show_name": show_name, "result": result})
            self.logger.log(f"Cleanup show {show_name!r}: {result}", step="14")
            time.sleep(0.6)
        return results

    def _cleanup_show_via_episode_list(
        self, show_url: str, show_name: str, video_nos: list[int]
    ) -> str:
        """Navigate to the show's episode list and remove each downloaded episode.

        For each video_no: hover row center → click ⋯ → 'Remove Download' via AX
        or keyboard fallback (Down×4+Enter).  No confirmation dialog for Remove Download.
        """
        if not show_url or not video_nos:
            return "no_info"

        self.open_url(show_url)
        self.activate()
        self.wait_for_window()
        # Give Podcasts time to load episode metadata through the VPN tunnel.
        time.sleep(3)

        see_all_status = self.click_see_all()
        if see_all_status in ("error", "see_all_not_found"):
            return f"see_all_failed:{see_all_status}"
        self.scroll_to_top()
        time.sleep(0.5)

        try:
            import Quartz  # type: ignore[import]
        except ImportError:
            return "quartz_unavailable"

        def _mouse(kind: int, x: int, y: int) -> None:
            pt = Quartz.CGPointMake(x, y)
            ev = Quartz.CGEventCreateMouseEvent(None, kind, pt, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.05)

        removed: list[int] = []
        for video_no in sorted(set(video_nos)):
            # Scroll to top before each BFS — episode list lazy-renders only the
            # visible rows, so the row counter is relative to the current viewport.
            self.scroll_to_top()
            time.sleep(0.3)
            out = run_osascript(
                self._episode_position_script(video_no),
                timeout=90,
                label=f"find episode {video_no} for removal",
            )

            # Scroll retry if needed (same logic as download)
            if out.startswith("ERROR:episode_not_found"):
                import re as _re
                seen_m = _re.search(r"seen=(\d+)", out)
                seen_n = int(seen_m.group(1)) if seen_m else 0
                if seen_n > 0:
                    row_h_est = 120
                    scroll_px = (video_no - seen_n + 2) * row_h_est
                    ev = Quartz.CGEventCreateScrollWheelEvent(
                        None, Quartz.kCGScrollEventUnitPixel, 1, -scroll_px
                    )
                    Quartz.CGEventSetLocation(ev, Quartz.CGPointMake(1060, 450))
                    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                    time.sleep(0.9)
                    out = run_osascript(
                        self._episode_position_script(video_no),
                        timeout=90,
                        label=f"find episode {video_no} for removal (retry)",
                    )

            if out.startswith("ERROR:"):
                self.logger.log(f"Cleanup episode {video_no}: row not found ({out})", step="14")
                continue

            row_x = row_y = row_w = row_h = more_x = more_y = 0
            for chunk in out.split("|"):
                if chunk.startswith("ROW:"):
                    parts = chunk[4:].split(",")
                    if len(parts) == 4:
                        try:
                            row_x, row_y, row_w, row_h = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
                        except ValueError:
                            pass
                elif chunk.startswith("MORE:"):
                    parts = chunk[5:].split(",")
                    if len(parts) == 2:
                        try:
                            more_x, more_y = int(parts[0]), int(parts[1])
                        except ValueError:
                            pass

            if row_w == 0 or more_x == 0:
                self.logger.log(f"Cleanup episode {video_no}: bad position in '{out}'", step="14")
                continue

            row_cx = row_x + row_w // 2
            row_cy = row_y + row_h // 2

            # Hover row center to reveal the ⋯ button, then click ⋯
            _mouse(Quartz.kCGEventMouseMoved, row_cx, row_cy)
            time.sleep(0.4)
            _mouse(Quartz.kCGEventLeftMouseDown, more_x, more_y)
            time.sleep(0.1)
            _mouse(Quartz.kCGEventLeftMouseUp, more_x, more_y)
            # Wait for Mac Catalyst context menu to render before key nav
            time.sleep(0.8)

            self.logger.log(
                f"Cleanup episode {video_no}: clicked ⋯ at ({more_x},{more_y})", step="14"
            )

            # Try AX direct menu selection first; keyboard fallback if menu not AX-accessible
            ax_ok = self._click_remove_menu_item_ax()
            if ax_ok:
                self.logger.log(
                    f"Cleanup episode {video_no}: AX menu selection used",
                    step="14",
                )
                self.state.data.setdefault("cleanup_menu_method", {}).update(
                    {str(video_no): "ax_direct"}
                )
            else:
                # Mac Catalyst ⋯ menus are not AX-accessible — keyboard nav fallback.
                # Down×1 selects 'Remove Download' (first item when episode is downloaded).
                # Enter activates it.  delay 0.3 between Down and Enter is required.
                import subprocess as _sp
                _sp.run(
                    ["osascript", "-e",
                     'tell application "System Events" to key code 125\n'
                     'delay 0.3\n'
                     'tell application "System Events" to key code 36'],
                    timeout=5, check=False,
                )
                self.logger.log(
                    f"Cleanup episode {video_no}: keyboard Down×1+Enter used (AX menu not accessible)",
                    step="14",
                )
                self.state.data["cleanup_fallback_keyboard_used"] = True
                self.state.data.setdefault("cleanup_menu_method", {}).update(
                    {str(video_no): "keyboard_fallback_down1_enter"}
                )

            removed.append(video_no)
            time.sleep(0.6)

        if not removed:
            return "no_episodes_removed"
        return f"removed_episodes:{','.join(str(v) for v in removed)}"

    def _episode_position_script(self, video_no: int) -> str:
        """Return the AppleScript that locates the Nth episode row position."""
        return f"""
        tell application "System Events"
            set frontmost of process "Podcasts" to true
        end tell
        delay 0.3
        tell application "System Events"
            tell process "Podcasts"
                if not (exists window 1) then return "ERROR:no_window"
                set targetN to {video_no}
                set seenCount to 0
                set targetEp to missing value
                set queue to {{window 1}}
                set deadline to (current date) + 75
                repeat 3000 times
                    if (count of queue) = 0 then exit repeat
                    if (current date) > deadline then return "ERROR:deadline_exceeded"
                    set elem to item 1 of queue
                    if (count of queue) > 1 then
                        set queue to items 2 thru -1 of queue
                    else
                        set queue to {{}}
                    end if
                    set isBtn to false
                    try
                        if class of elem is button then set isBtn to true
                    end try
                    if isBtn then
                        set looksLikeEpisode to false
                        try
                            set eSz to size of elem
                            set btnH to (item 2 of eSz) as integer
                            set btnW to (item 1 of eSz) as integer
                            if btnH > 60 and btnW > 400 then
                                set looksLikeEpisode to true
                            end if
                        end try
                        if looksLikeEpisode then
                            set seenCount to seenCount + 1
                            if seenCount = targetN then
                                set targetEp to elem
                                exit repeat
                            end if
                        end if
                    end if
                    try
                        repeat with ch in UI elements of elem
                            set end of queue to ch
                        end repeat
                    end try
                end repeat
                if targetEp is missing value then
                    return "ERROR:episode_not_found|seen=" & seenCount
                end if
                set ePos to position of targetEp
                set epSz to size of targetEp
                set eX to (item 1 of ePos) as integer
                set eY to (item 2 of ePos) as integer
                set eW to (item 1 of epSz) as integer
                set eH to (item 2 of epSz) as integer
                set moreX to 0
                set moreY to 0
                try
                    repeat with k in UI elements of targetEp
                        set kd to ""
                        try
                            set kd to description of k as string
                        end try
                        if kd is "more" then
                            set mp to position of k
                            set ms to size of k
                            set moreX to ((item 1 of mp) + (item 1 of ms) / 2) as integer
                            set moreY to ((item 2 of mp) + (item 2 of ms) / 2) as integer
                            exit repeat
                        end if
                        try
                            repeat with gk in UI elements of k
                                set gkd to ""
                                try
                                    set gkd to description of gk as string
                                end try
                                if gkd is "more" then
                                    set mp to position of gk
                                    set ms to size of gk
                                    set moreX to ((item 1 of mp) + (item 1 of ms) / 2) as integer
                                    set moreY to ((item 2 of mp) + (item 2 of ms) / 2) as integer
                                    exit repeat
                                end if
                            end repeat
                        end try
                        if moreX > 0 then exit repeat
                    end repeat
                end try
                return "ROW:" & eX & "," & eY & "," & eW & "," & eH & "|MORE:" & moreX & "," & moreY
            end tell
        end tell
        """

    # ── Show-name-targeted cleanup (Downloaded-tab card approach — fallback) ──────

    def cleanup_by_show_names(self, show_names: list[str]) -> list[dict[str, Any]]:
        """Remove each show by name from the Downloaded tab (fallback when no URL available)."""
        results: list[dict[str, Any]] = []
        for show_name in show_names:
            result = self._cleanup_show(show_name)
            results.append({"show_name": show_name, "result": result})
            self.logger.log(f"Cleanup show {show_name!r}: {result}", step="14")
            time.sleep(0.6)
        return results

    def _cleanup_show(self, show_name: str) -> str:
        """Remove one show by title from the Downloaded tab.

        Steps:
          1. Navigate to Downloaded tab.
          2. AX BFS: find element whose text contains show_name → climb to card parent.
          3. Find ⋯ button inside card (AX description/title contains 'more').
          4. Activate Podcasts → Quartz click ⋯.
          5. AX menu item 'Remove' first; keyboard Down×3+Enter as logged fallback.
          6. AX confirmation sheet → click Remove button.
        """
        nav = self.navigate_to_downloaded_tab()
        if nav not in ("navigated",):
            if nav == "quartz_unavailable":
                return "quartz_unavailable"
            return f"nav_failed:{nav}"
        time.sleep(0.5)

        # ── Find card by show name ────────────────────────────────────────────
        frame = self._find_downloaded_card_by_show_name(show_name)
        if frame is None:
            self._dump_ax_tree(f"cleanup_card_not_found_{show_name[:30]}")
            return "card_not_found"

        card_x, card_y, card_w, card_h = frame

        # ── Quartz click ⋯ at card bottom-right ──────────────────────────────
        try:
            import Quartz  # type: ignore[import]
        except ImportError:
            return "quartz_unavailable"

        def _mouse(kind, x, y):
            pt = Quartz.CGPointMake(x, y)
            ev = Quartz.CGEventCreateMouseEvent(None, kind, pt, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.05)

        def _key(vk, down):
            ev = Quartz.CGEventCreateKeyboardEvent(None, vk, down)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.05)

        # Activate Podcasts before any Quartz events
        try:
            run_osascript(
                'tell application "Podcasts" to activate',
                timeout=5, label="activate Podcasts before ⋯ click",
            )
            time.sleep(0.4)
        except AutomationError:
            pass

        card_cx = card_x + card_w // 2
        card_cy = card_y + card_h // 2
        three_dots_x = card_x + card_w - 30
        three_dots_y = card_y + card_h - 25

        self.logger.log(
            f"Card hover ({card_cx},{card_cy}) → ⋯ ({three_dots_x},{three_dots_y})",
            step="14",
        )
        _mouse(Quartz.kCGEventMouseMoved, card_cx, card_cy)
        time.sleep(0.4)
        _mouse(Quartz.kCGEventMouseMoved, three_dots_x, three_dots_y)
        time.sleep(0.2)
        _mouse(Quartz.kCGEventLeftMouseDown, three_dots_x, three_dots_y)
        time.sleep(0.1)
        _mouse(Quartz.kCGEventLeftMouseUp, three_dots_x, three_dots_y)
        time.sleep(0.8)

        # ── AX menu item 'Remove' first ───────────────────────────────────────
        remove_by_ax = self._click_remove_menu_item_ax()
        self.logger.log(f"AX Remove menu result: {remove_by_ax}", step="14")

        if not remove_by_ax:
            # Keyboard fallback — menu order: Follow/Unfollow / Report / Remove…
            self.logger.log(
                "AX menu not found — keyboard Down×3+Enter fallback",
                step="14", status="fallback_keyboard_remove_used",
            )
            self.state.data["cleanup_fallback_keyboard_used"] = True
            self.state.save()
            for _ in range(3):
                _key(0x7D, True); _key(0x7D, False)
                time.sleep(0.2)
            _key(0x24, True); _key(0x24, False)
            time.sleep(0.8)

        # ── Confirmation sheet ────────────────────────────────────────────────
        remove = self._click_confirmation_remove()
        time.sleep(0.5)
        self.logger.log(f"Confirmation sheet: {remove}", step="14")

        if "clicked" in remove:
            return "removed"
        elif remove == "no_sheet":
            return "no_confirm_dialog"
        return f"remove_failed:{remove}"

    def _find_downloaded_card_by_show_name(
        self, show_name: str
    ) -> tuple[int, int, int, int] | None:
        """Find the card container in the Downloaded view that has show_name as text.

        BFS: find a static text element whose value contains show_name (case-insensitive),
        then climb up the parent chain until we reach a container that is large enough
        (≥ 80 px per side) and lives in the content area (right of sidebar).
        Returns (x, y, w, h) or None.
        """
        safe_name = show_name.replace('"', '\\"').replace("'", "\\'")
        script = (
            """
        tell application "System Events"
            tell process "Podcasts"
                if not (exists window 1) then return "ERROR:no_window"
                set wPos to position of window 1
                set contentLeft to (item 1 of wPos) + 180
                set needle to "__SHOW_NAME__"
                set q to {window 1}
                set deadline to (current date) + 25
                repeat 3000 times
                    if (count of q) = 0 then exit repeat
                    if (current date) > deadline then exit repeat
                    set elem to item 1 of q
                    if (count of q) > 1 then
                        set q to items 2 thru -1 of q
                    else
                        set q to {}
                    end if
                    set eVal to ""
                    try
                        set eVal to value of elem as string
                    end try
                    if eVal is "" then
                        try
                            set eVal to name of elem as string
                        end try
                    end if
                    -- ignoring case avoids spawning a shell process per element
                    set matched to false
                    ignoring case
                        if eVal contains needle then set matched to true
                    end ignoring
                    if matched then
                        -- Found text match — climb up to card container
                        set candidate to elem
                        repeat 12 times
                            try
                                set cPos to position of candidate
                                set cSz to size of candidate
                                set cX to (item 1 of cPos) as integer
                                set cY to (item 2 of cPos) as integer
                                set cW to (item 1 of cSz) as integer
                                set cH to (item 2 of cSz) as integer
                                if cX > contentLeft and cW >= 80 and cH >= 80 then
                                    return "CARD:" & cX & "," & cY & "," & cW & "," & cH
                                end if
                                set candidate to parent of candidate
                            end try
                        end repeat
                    end if
                    try
                        repeat with ch in UI elements of elem
                            set end of q to ch
                        end repeat
                    end try
                end repeat
                return "NOCARD"
            end tell
        end tell
        """.replace("__SHOW_NAME__", safe_name)
        )
        try:
            out = run_osascript(script, timeout=35, label=f"find card for {show_name!r}")
        except AutomationError as exc:
            self.logger.log(f"_find_downloaded_card_by_show_name error: {exc}", step="14")
            return None

        if out.startswith("CARD:"):
            parts = out[5:].split(",")
            if len(parts) == 4:
                try:
                    return (int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]))
                except ValueError:
                    pass

        self.logger.log(
            f"Card not found for show {show_name!r} — AX result: {out[:80]}", step="14",
        )
        return None

    def _click_remove_menu_item_ax(self) -> bool:
        """Click the 'Remove…' context menu item via ApplicationServices AX walk + Quartz.

        The Podcasts Downloads card context menu exposes its items as AXButton elements
        readable via ApplicationServices (kAXDescriptionAttribute / kAXTitleAttribute).
        Locate the small button (h < 40) whose text contains 'Remove' or 'Delete' and
        click its pixel centre with Quartz.
        """
        try:
            import Quartz  # type: ignore[import]
        except ImportError:
            return False

        nodes = self._ax_nodes()
        for role, text, x, y, w, h in nodes:
            if (role == "AXButton" and h > 0 and h < 40
                    and ("Remove" in text or "Delete" in text)):
                cx = x + w // 2
                cy = y + h // 2
                pt = Quartz.CGPoint(x=float(cx), y=float(cy))
                for kind in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
                    ev = Quartz.CGEventCreateMouseEvent(
                        None, kind, pt, Quartz.kCGMouseButtonLeft)
                    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                    time.sleep(0.05)
                self.logger.log(
                    f"_click_remove_menu_item_ax: clicked '{text}' at ({cx},{cy})",
                    step="14",
                )
                return True
        return False

    # ── Generic card-based cleanup (fallback when show names not captured) ───

    def cleanup_all_downloaded(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        terminal_states = (
            "not_found",
            "no_window",
            "no_confirm_dialog",
            "quartz_unavailable",
            "card_not_found",
        )
        for i in range(50):
            res = self._cleanup_one_item()
            results.append({"iteration": i + 1, "result": res})
            if res in terminal_states or res.startswith("nav_failed") or res.startswith("remove_failed"):
                break
            time.sleep(0.6)
        return results

    def _cleanup_one_item(self) -> str:
        """Generic fallback: remove first card visible in Downloaded tab."""
        nav = self.navigate_to_downloaded_tab()
        if nav == "quartz_unavailable":
            return "quartz_unavailable"
        if nav not in ("navigated",):
            return f"nav_failed:{nav}"
        time.sleep(0.5)

        frame = self._find_downloaded_card_frame()
        if frame is None:
            self._dump_ax_tree("cleanup_generic_card_not_found")
            return "not_found"

        # Delegate to _cleanup_show using a placeholder name, reusing ⋯ + Remove logic
        card_x, card_y, card_w, card_h = frame
        try:
            import Quartz  # type: ignore[import]
        except ImportError:
            return "quartz_unavailable"

        def _mouse(kind, x, y):
            pt = Quartz.CGPointMake(x, y)
            ev = Quartz.CGEventCreateMouseEvent(None, kind, pt, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.05)

        def _key(vk, down):
            ev = Quartz.CGEventCreateKeyboardEvent(None, vk, down)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.05)

        try:
            run_osascript(
                'tell application "Podcasts" to activate',
                timeout=5, label="activate Podcasts before generic ⋯ click",
            )
            time.sleep(0.4)
        except AutomationError:
            pass

        card_cx = card_x + card_w // 2
        card_cy = card_y + card_h // 2
        three_dots_x = card_x + card_w - 30
        three_dots_y = card_y + card_h - 25

        _mouse(Quartz.kCGEventMouseMoved, card_cx, card_cy)
        time.sleep(0.8)
        _mouse(Quartz.kCGEventMouseMoved, three_dots_x, three_dots_y)
        time.sleep(0.4)
        _mouse(Quartz.kCGEventLeftMouseDown, three_dots_x, three_dots_y)
        time.sleep(0.1)
        _mouse(Quartz.kCGEventLeftMouseUp, three_dots_x, three_dots_y)
        time.sleep(0.8)

        remove_by_ax = self._click_remove_menu_item_ax()
        if not remove_by_ax:
            self.logger.log("Generic cleanup: keyboard fallback", step="14",
                            status="fallback_keyboard_remove_used")
            self.state.data["cleanup_fallback_keyboard_used"] = True
            self.state.save()
            for _ in range(3):
                _key(0x7D, True); _key(0x7D, False)
                time.sleep(0.2)
            _key(0x24, True); _key(0x24, False)
            time.sleep(0.8)

        remove = self._click_confirmation_remove()
        time.sleep(0.5)

        if "clicked" in remove:
            return "removed"
        elif remove == "no_sheet":
            return "no_confirm_dialog"
        return f"remove_failed:{remove}"

    def cleanup_all_from_downloads_tab(
        self, expected_cards: int | None = None
    ) -> list[dict[str, Any]]:
        """Remove all downloaded shows directly from the Downloads tab.

        The Downloads tab displays show cards (artwork squares, roughly 80–450 px per
        side).  Strategy: navigate to Downloaded, then loop:
          1. Find the first show card via _find_downloaded_card_frame (BFS, card geometry).
          2. Hover the card center to reveal the ⋯ button (bottom-right corner).
          3. Click ⋯, wait 1.2s for the Mac Catalyst context menu.
          4. Try AX click on 'Remove' / 'Delete' menu item; keyboard Down+Enter fallback.
          5. If Podcasts shows a confirmation sheet, click Remove in it.
          6. Re-navigate to Downloaded and repeat.

        `expected_cards`: if given (the number of shows we actually downloaded this
        cycle), stop after removing that many — this skips the expensive
        "is the grid empty?" card search at the end (each such search is a ~30s
        System Events AX walk, and it ran 2-3× per cleanup before).
        """
        try:
            import Quartz  # type: ignore[import]
        except ImportError:
            return [{"iteration": 0, "result": "quartz_unavailable"}]

        def _mouse(kind: int, x: int, y: int) -> None:
            pt = Quartz.CGPointMake(x, y)
            ev = Quartz.CGEventCreateMouseEvent(None, kind, pt, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.05)

        results: list[dict[str, Any]] = []
        removed = 0
        for iteration in range(50):
            # Stop as soon as we've removed every card we expected — avoids the
            # trailing empty-grid search(es), which dominate cleanup time.
            if expected_cards is not None and removed >= expected_cards:
                self.logger.log(
                    f"Downloads cleanup: removed expected {removed} card(s) — done",
                    step="14",
                )
                results.append({"iteration": iteration + 1, "result": "done_expected_count"})
                break

            # Re-navigate each iteration — card removal may shift view focus.
            nav = self.navigate_to_downloaded_tab()
            if nav != "navigated":
                self.logger.log(f"Downloads cleanup: nav failed ({nav})", step="14")
                results.append({"iteration": iteration + 1, "result": f"nav_failed:{nav}"})
                break

            frame = self._find_downloaded_card_frame()
            if frame is None:
                # Retry once — card may still be rendering after navigation.
                time.sleep(0.5)
                frame = self._find_downloaded_card_frame()
            if frame is None:
                # No card found — determine whether the grid is empty or we landed on
                # a show's episode page instead of the Downloads grid.  The episode page
                # has a "Back" button in the nav bar; the grid does not.
                if self._has_back_button():
                    self.logger.log(
                        "Downloads cleanup: on show page — clicking Back to reach grid",
                        step="14",
                    )
                    self._click_back_button()
                    time.sleep(0.8)
                    frame = self._find_downloaded_card_frame()
                    if frame is None:
                        time.sleep(0.5)
                        frame = self._find_downloaded_card_frame()
            if frame is None:
                self.logger.log(
                    f"Downloads cleanup: no more show cards after {iteration} removal(s)",
                    step="14",
                )
                results.append({"iteration": iteration + 1, "result": "done"})
                break

            card_x, card_y, card_w, card_h = frame
            # Artwork on Downloads grid cards is always a square whose side equals
            # the card width.  The ⋯ button appears at the lower-right of the artwork
            # square (not the lower-right of the full card which includes the title strip
            # below the artwork).
            artwork_h = card_w
            three_x = card_x + card_w - 20       # 20 px inside right edge of artwork
            three_y = card_y + artwork_h - 20     # 20 px above bottom of artwork square
            artwork_cx = card_x + card_w // 2
            artwork_cy = card_y + artwork_h // 2

            self.logger.log(
                f"Downloads cleanup card {iteration + 1}: ({card_x},{card_y},{card_w},{card_h}) "
                f"artwork_cx=({artwork_cx},{artwork_cy}) three_dots=({three_x},{three_y})",
                step="14",
            )

            # Bring Podcasts to front explicitly before any mouse/key events.
            try:
                run_osascript(
                    'tell application "Podcasts" to activate',
                    timeout=5, label="activate Podcasts before cleanup click",
                )
                time.sleep(0.3)
            except AutomationError:
                pass

            def _warp(x: int, y: int) -> None:
                pt_w = Quartz.CGPoint(x=float(x), y=float(y))
                Quartz.CGWarpMouseCursorPosition(pt_w)
                mv = Quartz.CGEventCreateMouseEvent(
                    None, Quartz.kCGEventMouseMoved, pt_w, Quartz.kCGMouseButtonLeft
                )
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, mv)
                time.sleep(0.05)

            def _key(vk: int, down: bool) -> None:
                ev = Quartz.CGEventCreateKeyboardEvent(None, vk, down)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                time.sleep(0.07)

            def _open_three_dots_menu() -> None:
                """Hover artwork center → move to ⋯ → left-click to open context menu."""
                _warp(artwork_cx, artwork_cy)
                time.sleep(0.8)    # hover so the ⋯ button renders
                _warp(three_x, three_y)
                time.sleep(0.3)
                Quartz.CGAssociateMouseAndMouseCursorPosition(True)
                pt_td = Quartz.CGPoint(x=float(three_x), y=float(three_y))
                for kind in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
                    ev_td = Quartz.CGEventCreateMouseEvent(
                        None, kind, pt_td, Quartz.kCGMouseButtonLeft
                    )
                    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev_td)
                    time.sleep(0.05)
                time.sleep(1.2)   # Mac Catalyst context menu render time

            _open_three_dots_menu()

            # AX click on the 'Remove…' menu item via ApplicationServices + Quartz.
            ax_ok = self._click_remove_menu_item_ax()
            confirm = "no_sheet"
            actual_removed = False

            if not ax_ok:
                self.logger.log(
                    f"Downloads cleanup card {iteration + 1}: Remove item not found via AX",
                    step="14",
                )
                results.append({"iteration": iteration + 1, "result": "remove_not_found"})
                break

            time.sleep(0.4)
            confirm = self._click_confirmation_remove()
            actual_removed = True

            result_label = "removed:ax"
            if confirm not in ("no_sheet",):
                result_label += f"+confirmed:{confirm}"

            self.logger.log(
                f"Downloads cleanup card {iteration + 1}: {result_label}", step="14"
            )
            results.append({"iteration": iteration + 1, "result": result_label})
            if actual_removed:
                removed += 1
            # No settle here — move straight on to the next show. (The card finder
            # below retries if the next card hasn't rendered yet.)

        return results

    def quit_app(self) -> None:
        # Both PyXA.quit() and `osascript ... quit` are GRACEFUL but SYNCHRONOUS:
        # they block until Podcasts has fully terminated, which can take 10-30s after
        # a cleanup pass (the app flushes its library/download state on the way out).
        # That blocking was the entire "slow to quit" delay. Fire the quit
        # asynchronously instead — the AppleScript still performs a clean quit, but
        # the run continues immediately while Podcasts winds down in the background.
        try:
            subprocess.Popen(
                ["osascript", "-e", 'tell application "Podcasts" to quit'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self.logger.log("Requested Podcasts quit (async)", step="15")
        except Exception as exc:
            self.logger.log(f"quit_app async launch failed: {exc}", step="15")


# -----------------------------------------------------------------------------
# Orchestrator
# -----------------------------------------------------------------------------
class Orchestrator:
    def __init__(self, config: Config, log_dir: Path, state_path: Path):
        self.config = config
        self.logger = RunLogger(log_dir)
        self.state = StateManager(state_path)
        self.net = NetworkState(self.logger)
        self.vpn = VPNController(self.logger, self.net, self.state)
        self.chrome = ChromeController(self.logger, self.state)
        self.podcasts = PodcastsController(self.logger, self.state)

    def run(self) -> int:
        self.logger.log("Started podcast automation", step="01")
        try:
            self.logger.log(
                f"Loaded minimal input: repeat={self.config.repeat} vpn={self.config.vpn} "
                f"cleanup={self.config.cleanup} cleanup_mode={self.config.cleanup_mode} "
                f"clean_start={self.config.clean_start} tabs={len(self.config.tabs)}",
                step="02",
            )
            self.logger.log(f"Loaded runtime state: {self.state.path}", step="03",
                            completed_cycles=self.state.data["completed_cycles"])

            # If every cycle in the current config is already marked complete, the
            # previous run finished normally — this is a fresh re-run, not a resume.
            # Reset run-specific fields so cycles execute again.
            # Preserve VPN discovery and rotation history across runs.
            all_expected = set(range(1, self.config.repeat + 1))
            already_done = set(self.state.data.get("completed_cycles", []))
            if all_expected and all_expected.issubset(already_done):
                self.logger.log(
                    f"All {self.config.repeat} cycle(s) were completed in a previous run — "
                    f"resetting state for fresh run",
                    step="03", status="state_reset_for_fresh_run",
                )
                for _key in (
                    "completed_cycles", "cycle_phases", "processed_shows",
                    "podcast_task_results", "download_check_results", "cleanup_results",
                    "see_all_state", "vpn_sessions", "chrome_tabs_cache",
                ):
                    self.state.data[_key] = [] if isinstance(
                        self.state.data.get(_key), list) else {}
                self.state.data.update(
                    current_cycle=None, current_tab=None, current_video=None,
                    last_failed_step=None, last_error=None, resume_available=False,
                )
                self.state.save()

            self.run_preflight()  # comprehensive: platform, apps, AX, dirs, Chrome tab count

            tabs_cache = self.state.data.get("chrome_tabs_cache", {})
            if not tabs_cache:
                self.chrome.activate()
                tabs_cache = self.chrome.enumerate_tabs()
            self.logger.log(f"Chrome tabs: {len(tabs_cache)} found", step="04",
                            tab_count=len(tabs_cache))
            self._preflight_chrome_tasks(tabs_cache)

            # Optional startup cleanup: remove any stale downloaded items left by a
            # previous failed run before the first download cycle begins.
            if self.config.clean_start:
                self._startup_cleanup()

            for cycle in range(1, self.config.repeat + 1):
                if cycle in self.state.data["completed_cycles"]:
                    self.logger.log(f"Cycle {cycle} already completed — skipping", step="05",
                                    cycle=cycle, status="skipped_resume")
                    continue

                # Inspect phase checkpoints from a previous interrupted run so we can
                # resume mid-cycle without re-doing downloads that already succeeded.
                cycle_phases = self.state.data.get("cycle_phases", {}).get(str(cycle), {})
                all_tabs_done = "all_tabs_completed" in cycle_phases
                cleanup_started = "cleanup_started" in cycle_phases
                cleanup_done = "cleanup_completed" in cycle_phases
                # If downloads finished but script crashed before/during cleanup, skip to cleanup.
                skip_to_cleanup = (all_tabs_done or cleanup_started) and not cleanup_done

                self.state.data["last_failed_step"] = None
                self.state.data["last_error"] = None
                self.state.update(current_cycle=cycle)
                self.logger.log(f"Starting cycle {cycle}", step="05", cycle=cycle,
                                skip_to_cleanup=skip_to_cleanup)
                self.state.mark_phase(cycle, "cycle_started")

                if skip_to_cleanup:
                    self.logger.log(
                        f"Cycle {cycle}: downloads already completed, resuming cleanup",
                        step="05", cycle=cycle, status="resume_cleanup",
                    )
                else:
                    if self.config.vpn.enabled:
                        self.vpn.connect_with_config(cycle=cycle, vpn_cfg=self.config.vpn)
                    else:
                        self.logger.log("VPN disabled", step="06", status="vpn_disabled")

                # Whether downloads are already confirmed complete before cleanup.
                # On a resume that skips straight to cleanup we don't know, so the
                # cleanup phase will fall back to its own download wait.
                downloads_done = False

                if not skip_to_cleanup:
                    for tab_task in self.config.tabs:
                        self._process_tab(tab_task, cycle)

                    self.state.mark_phase(cycle, "all_tabs_completed")

                    # Open the Downloading progress page now that every show's
                    # episodes are queued, then wait (doing nothing else) for that
                    # modal to auto-close — its disappearance means every episode
                    # finished downloading, so cleanup can start right away.
                    try:
                        dl_result = self.podcasts.show_downloading_page()
                        downloads_done = dl_result in ("completed", "no_downloading_item")
                    except Exception as exc:
                        self.logger.log(
                            f"show_downloading_page error (non-fatal): {exc}",
                            step="13",
                        )

                if self.config.cleanup:
                    self._cleanup_phase(cycle, downloads_already_done=downloads_done)

                self.podcasts.quit_app()

                completed = list(self.state.data["completed_cycles"])
                completed.append(cycle)
                self.state.update(completed_cycles=completed,
                                  current_tab=None, current_video=None)
                self.state.mark_phase(cycle, "cycle_completed")
                self.logger.log(f"Cycle {cycle} complete", step="15", cycle=cycle)

            self.logger.log("All cycles complete", step="16")
            return 0

        except Exception as exc:
            step = self.state.data.get("last_failed_step") or "ERROR"
            self.state.record_failure(step=step, error=str(exc))
            self.logger.log(f"Automation failed at step {step}: {exc}", step="ERROR", error=str(exc))
            return 1
        finally:
            self.logger.save_report(state=self.state.data)

    def _validate_environment(self) -> None:
        self.run_preflight()

    def run_preflight(self) -> None:
        """Comprehensive preflight checks. Raises AutomationError on first failure."""
        failures: list[str] = []

        # Platform
        if platform.system() != "Darwin":
            raise AutomationError("This script must run on macOS")

        # Python version
        if sys.version_info < (3, 9):
            failures.append(f"Python ≥ 3.9 required (got {sys.version.split()[0]})")

        # Required apps
        required_apps = {"Google Chrome": "Google Chrome", "Podcasts": "Podcasts"}
        if self.config.vpn.enabled:
            required_apps[self.config.vpn.app] = self.config.vpn.app
        missing_apps = [name for name in required_apps if not self._app_available(name)]
        if missing_apps:
            failures.append(f"Required app not found: {', '.join(missing_apps)}")

        # Accessibility permission — attempt a harmless AX operation
        ax_ok = False
        try:
            out = run_osascript(
                'tell application "System Events" to tell process "Finder" to get exists',
                timeout=6, label="ax_permission_check",
            )
            ax_ok = True
        except AutomationError as exc:
            if "25211" in str(exc) or "assistive" in str(exc).lower():
                failures.append(
                    "Accessibility permission not granted.\n"
                    "  Fix: System Settings → Privacy & Security → Accessibility\n"
                    "       Enable Terminal (or your launcher), then re-run."
                )
            else:
                ax_ok = True  # different error; AX itself may be fine

        # Writable directories
        for d in (self.logger.log_path.parent, self.state.path.parent):
            try:
                d.mkdir(parents=True, exist_ok=True)
                test = d / ".preflight_write_test"
                test.write_text("ok")
                test.unlink()
            except OSError as exc:
                failures.append(f"Directory not writable ({d}): {exc}")

        env = {
            "platform": platform.system(),
            "python": sys.version.split()[0],
            "has_pyxa": HAS_PYXA,
            "ax_permission": ax_ok,
        }
        self.state.data["environment_checks"] = {"apps": {k: k not in missing_apps for k in required_apps}, **env}
        self.state.save()

        if failures:
            raise AutomationError("Preflight failed:\n" + "\n".join(f"  • {f}" for f in failures))

        self.logger.log(f"Preflight OK: {env}", step="03", **env)

        # Chrome tab count check (done after AX / app checks pass)
        if ax_ok and "Google Chrome" not in missing_apps:
            try:
                tabs_cache = self.chrome.enumerate_tabs()
                max_tab = max((t.tab for t in self.config.tabs), default=1)
                if len(tabs_cache) < max_tab:
                    raise AutomationError(
                        f"Input requests tab {max_tab} but Chrome only has {len(tabs_cache)} tab(s). "
                        "Open the Apple Podcasts pages in Chrome first."
                    )
            except AutomationError:
                raise
            except Exception as exc:
                self.logger.log(f"Chrome tab count check warning: {exc}", step="03")

    def _preflight_chrome_tasks(self, tabs_cache: dict[str, dict[str, str]]) -> None:
        for tab_task in self.config.tabs:
            cached = tabs_cache.get(str(tab_task.tab))
            if not cached:
                self.state.record_failure(
                    step="04",
                    error=f"configured_tab_missing:{tab_task.tab}",
                    current_tab=tab_task.tab,
                )
                raise AutomationError(
                    f"Configured Chrome tab {tab_task.tab} was not found. "
                    f"Detected tabs: {', '.join(tabs_cache.keys()) or 'none'}"
                )
            url = cached.get("url", "")
            title = cached.get("title", "")
            if APPLE_PODCASTS_HOST not in url:
                self.state.record_failure(
                    step="04",
                    error="not_apple_podcasts_url",
                    current_tab=tab_task.tab,
                    active_url=url,
                    active_title=title,
                )
                raise AutomationError(
                    f"Configured Chrome tab {tab_task.tab} is not an Apple Podcasts URL: {url}"
                )

    @staticmethod
    def _app_available(app_name: str) -> bool:
        candidates = [app_name, app_name.replace(" ", "")]
        for name in candidates:
            if (Path("/Applications") / f"{name}.app").exists():
                return True
        for name in candidates:
            try:
                proc = subprocess.run(
                    ["osascript", "-e", f'id of application "{name}"'],
                    text=True,
                    capture_output=True,
                    timeout=6,
                )
            except subprocess.TimeoutExpired:
                continue
            if proc.returncode == 0:
                return True
        return False

    def diagnose_live(self) -> dict[str, Any]:
        self.logger.log("Started live diagnostic", step="01")
        result: dict[str, Any] = {
            "platform": platform.system(),
            "has_pyxa": HAS_PYXA,
            "apps": {},
            "chrome": {},
            "vpn": {},
            "podcasts": {},
        }
        try:
            self._validate_environment()
            result["apps"] = self.state.data.get("environment_checks", {}).get("apps", {})
        except Exception as exc:
            result["environment_error"] = str(exc)

        try:
            self.chrome.activate()
            tabs_cache = self.chrome.enumerate_tabs()
            result["chrome"] = {
                "status": "ok",
                "tab_count": len(tabs_cache),
                "tabs": tabs_cache,
            }
            self.logger.log(f"Diagnostic Chrome tabs: {len(tabs_cache)}", step="04")
        except Exception as exc:
            result["chrome"] = {"status": "error", "error": str(exc)}
            self.logger.log(f"Diagnostic Chrome failed: {exc}", step="04")

        if self.config.vpn.enabled:
            try:
                result["vpn"] = self.vpn.diagnose_current_state(self.config.vpn)
            except Exception as exc:
                result["vpn"] = {"status": "error", "error": str(exc)}
        else:
            result["vpn"] = {"status": "disabled"}

        try:
            result["podcasts"] = {"episode_list_state": self.podcasts.episode_list_state()}
        except Exception as exc:
            result["podcasts"] = {"status": "error", "error": str(exc)}

        self.state.data["last_live_diagnostic"] = result
        self.state.save()
        self.logger.log(f"Live diagnostic complete: {result}", step="03", **result)
        return result

    def _process_tab(self, tab_task: TabTask, cycle: int) -> None:
        self.state.update(current_tab=tab_task.tab, current_video=None)
        self.logger.log(f"Switching Chrome to tab {tab_task.tab}", step="07", tab=tab_task.tab)
        title, url = self.chrome.switch_tab(tab_task.tab)
        self.logger.log(f"Active tab URL detected: {url}", step="08",
                        tab=tab_task.tab, title=title, url=url)

        if APPLE_PODCASTS_HOST not in url:
            self.state.record_failure(step="08", error="not_apple_podcasts_url",
                                      current_tab=tab_task.tab)
            raise AutomationError(f"Tab {tab_task.tab} is not an Apple Podcasts URL: {url}")

        # Normalize episode URLs (?i=...) to the show URL so Podcasts opens
        # the full episode list (which has a 'See All') instead of a single
        # episode page (which doesn't).
        podcast_url = url.split("?i=")[0] if "?i=" in url else url
        if podcast_url != url:
            self.logger.log(
                f"Episode URL detected; opening show page instead: {podcast_url}",
                step="08", status="url_normalized",
                original_url=url, opened_url=podcast_url,
            )

        self.logger.log(f"Opening URL in Podcasts app: {podcast_url}", step="09")
        self.podcasts.open_url(podcast_url)
        self.podcasts.activate()
        self.podcasts.wait_for_window()
        # No fixed settle: click_see_all() already polls for the element to render,
        # so any wait here is dead time before that poll even starts.
        self.logger.log("Podcasts page loaded", step="10")

        # Capture show name for state-driven cleanup.
        # Primary: Chrome tab title (most reliable — e.g. "The Daily - Apple Podcasts").
        # Secondary: Podcasts AX window title / heading.
        chrome_title = title.strip()
        _podcast_suffixes = (
            " - Apple Podcasts", " – Apple Podcasts", " — Apple Podcasts",
            " | Apple Podcasts",
            " - Podcast", " – Podcast", " — Podcast",
            " - Podcasts", " – Podcasts", " — Podcasts",
        )
        _changed = True
        while _changed:
            _changed = False
            for _sfx in _podcast_suffixes:
                if chrome_title.endswith(_sfx):
                    chrome_title = chrome_title[: -len(_sfx)].strip()
                    _changed = True
                    break
        if chrome_title and chrome_title.lower() not in ("", "podcasts", "apple podcasts"):
            show_name = chrome_title
        else:
            show_name = self.podcasts.capture_show_name()
        self.logger.log(f"Show name captured: {show_name!r}", step="10", show_name=show_name)

        # Record this tab in processed_shows so cleanup can find it by name
        shows = self.state.data.setdefault("processed_shows", {})
        cycle_shows: list[dict[str, Any]] = shows.setdefault(str(cycle), [])
        show_entry: dict[str, Any] = {
            "tab": tab_task.tab,
            "url": podcast_url,
            "show_name": show_name,
            "videos_requested": list(tab_task.videos),
            "videos_downloaded": [],
        }
        cycle_shows.append(show_entry)
        self.state.save()

        see_all_result = self.podcasts.click_see_all()
        self.logger.log(f"See All {see_all_result}", step="11", status=see_all_result)
        self.state.data.setdefault("see_all_state", {})[str(tab_task.tab)] = see_all_result
        self.state.save()
        if see_all_result not in ("clicked",) and not see_all_result.startswith("list_already_expanded"):
            list_state = self.podcasts.episode_list_state(min_rows=max(tab_task.videos))
            self.logger.log(f"Episode list state after missing See All: {list_state}",
                            step="11", status=list_state, tab=tab_task.tab)
            self.state.data.setdefault("see_all_state", {})[str(tab_task.tab)] = list_state
            self.state.save()
            if not list_state.startswith("list_already_expanded"):
                self.state.record_failure(
                    step="11",
                    error="see_all_not_found",
                    current_tab=tab_task.tab,
                    active_url=url,
                    active_title=title,
                )
                raise AutomationError(
                    f"See All not found and episode list is not visible on tab {tab_task.tab}: {list_state}"
                )

        self.podcasts.scroll_to_top()
        self.logger.log("Episode list reset to top", step="12")

        # Download every requested episode of this show with a SINGLE AX tree-walk:
        # download_episode_rows measures all the rows up-front and then pixel-clicks
        # each, instead of scrolling to top and re-walking the tree once per episode
        # (which was the ~30s-per-episode latency between downloads).
        self.logger.log(
            f"Downloading episodes {list(tab_task.videos)} (single-pass)",
            step="13", videos=list(tab_task.videos),
        )
        statuses = self.podcasts.download_episode_rows(list(tab_task.videos))
        for video_no in tab_task.videos:
            self.state.update(current_video=video_no)
            status = statuses.get(video_no, "download_not_found")
            self.logger.log(f"Target video {video_no} {status}", step="13",
                            video=video_no, status=status)
            self.state.mark_phase(cycle, f"video_{tab_task.tab}_{video_no}_{status}")
            # Track as downloaded for cleanup purposes regardless of whether the
            # download was newly triggered or was already present on the device.
            if status in ("download_clicked", "already_downloaded",
                          "already_downloaded_popup_dismissed"):
                show_entry["videos_downloaded"].append(video_no)
            self.state.save()
            self.state.add_task_result(
                cycle=cycle, tab=tab_task.tab, video=video_no,
                status=status, url=url, title=title, show_name=show_name,
            )

        self.state.mark_phase(cycle, f"tab_{tab_task.tab}_completed")

    def _download_check_phase(self, cycle: int) -> None:
        self.logger.log("Opening Downloaded sidebar for download check", step="14")
        opened = self.podcasts.open_downloaded_sidebar()
        check: dict[str, Any] = {"cycle": cycle, "opened": opened}
        if opened == "downloaded_opened":
            check.update(self.podcasts.check_downloads_state())
        else:
            check.update({"status": "downloaded_sidebar_not_accessible", "count": 0})
        self.state.add_download_check_result(**check)
        self.logger.log(f"Download check: {check}", step="14", **check)

    def _startup_cleanup(self) -> None:
        """Remove stale downloaded items before the first cycle begins.

        Called only when clean_start=True in tasks.json.
        """
        self.logger.log("clean_start: checking Downloads tab for stale items", step="00")
        try:
            self.podcasts.activate()
            self.podcasts.wait_for_window()
            results = self.podcasts.cleanup_all_from_downloads_tab()
            removed = sum(1 for r in results if "removed" in r.get("result", ""))
            self.logger.log(
                f"clean_start cleanup done: {removed} episode(s) removed", step="00"
            )
        except Exception as exc:
            self.logger.log(f"clean_start cleanup error (non-fatal): {exc}", step="00")

    def _cleanup_phase(self, cycle: int, downloads_already_done: bool = False) -> None:
        self.state.mark_phase(cycle, "cleanup_started")
        self.podcasts.activate()
        self.podcasts.wait_for_window()
        self.logger.log("Cleanup phase start", step="14", cycle=cycle)

        if downloads_already_done:
            # show_downloading_page() already watched the Downloads modal close, so
            # every episode is finished — no need to re-poll. Start removing now.
            self.logger.log(
                "Downloads already confirmed complete (Downloading modal closed) — "
                "skipping download wait",
                step="14", cycle=cycle,
            )
        else:
            # Wait for all in-progress downloads to finish before removing anything.
            dl_status = self.podcasts.wait_for_downloads_stable(timeout=180)
            self.logger.log(
                f"Download wait: {dl_status} "
                f"(state={self.state.data.get('download_state')} "
                f"waited={self.state.data.get('download_wait_seconds')}s)",
                step="14", cycle=cycle,
            )
        self.state.mark_phase(cycle, "downloads_stable")

        # Each downloaded show is one card on the Downloads tab. We know how many we
        # queued this cycle, so tell cleanup to stop after removing exactly that many
        # — this skips the ~30s-each empty-grid searches that used to run at the end.
        shows = self.state.data.get("processed_shows", {}).get(str(cycle), [])
        expected_cards = sum(1 for s in shows if s.get("videos_downloaded")) or None

        # Remove every downloaded show from the Downloads tab.
        results = self.podcasts.cleanup_all_from_downloads_tab(expected_cards=expected_cards)

        for r in results:
            self.state.add_cleanup_result(cycle=cycle, **r)

        removed = sum(1 for r in results if "removed" in r.get("result", ""))
        self.state.mark_phase(cycle, "cleanup_completed")
        self.logger.log(
            f"Cleanup finished: {removed} episode(s) removed ({len(results)} actions)",
            step="14", cycle=cycle, action_count=len(results), removed_count=removed,
        )


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Apple Podcasts automation (state-driven, minimal-input)"
    )
    parser.add_argument("--input", type=Path, default=Path("input/tasks.json"),
                        help="Path to minimal tasks JSON")
    parser.add_argument("--state", type=Path, default=Path("state/runtime_state.json"),
                        help="Runtime state file (auto-created)")
    parser.add_argument("--output-dir", type=Path, default=Path("logs"),
                        help="Logs and reports directory")
    parser.add_argument("--diagnose-vpn", action="store_true",
                        help="Only inspect current VPN/network state; do not connect or download")
    parser.add_argument("--test-vpn-connect", action="store_true",
                        help="Connect and verify the configured VPN only; do not use Chrome or Podcasts")
    parser.add_argument("--diagnose-live", action="store_true",
                        help="Inspect apps, Chrome tabs, VPN/network, and Podcasts UI without downloads")
    parser.add_argument("--diagnose-ax", action="store_true",
                        help="Dump Podcasts AX tree (Downloaded tab state) to logs/ without running automation")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.input)
    except AutomationError as exc:
        print(f"\nConfiguration error:\n{exc}\n", file=sys.stderr)
        return 1
    if args.diagnose_vpn:
        logger = RunLogger(args.output_dir)
        state = StateManager(args.state)
        net = NetworkState(logger)
        vpn = VPNController(logger, net, state)
        logger.log("Started VPN diagnostic", step="01")
        result = vpn.diagnose_current_state(config.vpn)
        logger.log(
            f"VPN diagnostic: verified_connected={result['verified_connected']} "
            f"reason={result['reason']} ui={result['ui_connection_state']} "
            f"network={result['network']}",
            step="06",
            **result,
        )
        logger.save_report(state=state.data)
        return 0 if result["verified_connected"] else 2

    if args.test_vpn_connect:
        logger = RunLogger(args.output_dir)
        state = StateManager(args.state)
        net = NetworkState(logger)
        vpn = VPNController(logger, net, state)
        logger.log("Started VPN connect test", step="01")
        if not config.vpn.enabled:
            logger.log("VPN disabled in input; nothing to connect", step="06", status="vpn_disabled")
            logger.save_report(state=state.data)
            return 2
        try:
            state.data["last_failed_step"] = None
            state.data["last_error"] = None
            state.save()
            result = vpn.connect_with_config(cycle=max(1, int(state.data.get("current_cycle") or 1)),
                                             vpn_cfg=config.vpn)
            logger.log(f"VPN connect test finished: {result}", step="06", status=result)
            logger.save_report(state=state.data)
            return 0
        except Exception as exc:
            state.record_failure(step="06", error=str(exc))
            logger.log(f"VPN connect test failed: {exc}", step="ERROR", error=str(exc))
            logger.save_report(state=state.data)
            return 1

    if args.diagnose_ax:
        logger = RunLogger(args.output_dir)
        state = StateManager(args.state)
        podcasts = PodcastsController(logger, state)
        logger.log("AX diagnostic: activating Podcasts and navigating to Downloaded", step="01")
        try:
            podcasts.activate()
            podcasts.wait_for_window(timeout_sec=10)
            podcasts.navigate_to_downloaded_tab()
            time.sleep(1.5)
        except Exception as exc:
            logger.log(f"AX diagnostic setup warning: {exc}", step="01")
        dump_path = podcasts._dump_ax_tree("diagnose_ax_downloaded", max_depth=8, max_elements=1000)
        logger.log(f"AX diagnostic complete: {dump_path}", step="01")
        logger.save_report(state=state.data)
        print(f"\nAX dump saved to: {dump_path}")
        return 0

    if args.diagnose_live:
        orch = Orchestrator(config, log_dir=args.output_dir, state_path=args.state)
        result = orch.diagnose_live()
        orch.logger.save_report(state=orch.state.data)
        has_errors = any(
            isinstance(value, dict) and value.get("status") == "error"
            for value in result.values()
        )
        if result.get("environment_error"):
            has_errors = True
        return 1 if has_errors else 0

    orch = Orchestrator(config, log_dir=args.output_dir, state_path=args.state)
    return orch.run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
