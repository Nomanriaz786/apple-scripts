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
# Hard ceiling on total wall-clock time connect_with_config will spend rotating
# through servers for ONE cycle. Every individual VPN operation already has its
# own bounded timeout, so this loop cannot literally hang — but a large server
# list where every attempt fails slowly (e.g. a stale/mismatched AX lookup on a
# different Mac) could otherwise churn for a very long time before giving up.
# This bounds the worst case independent of how many servers are in the list.
DEFAULT_VPN_CYCLE_BUDGET_SEC = 900
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
    """Legacy per-device pixel geometry, from the earlier ProtonVPN Quartz/hover
    based connect routine (see git history). Unused by the current NordVPN
    connector: its location list, like Surfshark's before it, is driven by
    real AX row/element coordinates read fresh each time rather than fixed
    per-device pixel offsets — kept only so an existing `vpn.calibration`
    block in input/tasks.json still parses harmlessly instead of raising.
    """
    state_connect_offset_from_right: int = 110  # window_right_edge - Connect_btn_x on state row
    header_height: int = 48                     # US country-header row height
    row_height: int = 48                        # individual state/server row height


@dataclass(frozen=True)
class VPNConfig:
    enabled: bool
    app: str = "NordVPN"
    location: str = "United States"
    location_code: str = "US"
    servers: tuple[str, ...] = ()
    require_provider_in_org: bool = True
    verify_timeout: int = DEFAULT_VERIFY_TIMEOUT_SEC
    calibration: VPNCalibration = field(default_factory=VPNCalibration)


@dataclass(frozen=True)
class AccountEntry:
    email: str
    password: str


@dataclass(frozen=True)
class Config:
    repeat: int
    vpn: VPNConfig
    cleanup: bool
    tabs: list[TabTask]
    check_downloads: bool = False
    clean_start: bool = False
    cleanup_mode: str = "remove_download"  # "remove_download" | "remove_from_library"
    accounts: tuple["AccountEntry", ...] = ()


def _parse_vpn(raw_vpn: Any) -> VPNConfig:
    if raw_vpn is None or raw_vpn is False:
        return VPNConfig(enabled=False)
    if raw_vpn is True:
        return VPNConfig(enabled=True)
    if not isinstance(raw_vpn, dict):
        raise ValueError("'vpn' must be a boolean or an object {enabled, app, location}")

    enabled = bool(raw_vpn.get("enabled", True))
    app = str(raw_vpn.get("app", "NordVPN")).strip() or "NordVPN"
    location = str(raw_vpn.get("location", "United States")).strip() or "United States"
    location_code = _COUNTRY_CODE_FALLBACK.get(location.lower(), location.upper()[:2] or "US")

    servers_raw = raw_vpn.get("servers", [])
    if not isinstance(servers_raw, list):
        raise ValueError("'vpn.servers' must be a list of strings (optional explicit override)")
    servers = tuple(str(s).strip() for s in servers_raw if str(s).strip())

    require_default = "proton" in app.lower() or "surfshark" in app.lower()
    require = bool(raw_vpn.get("require_provider_in_org", require_default))

    cal_raw = raw_vpn.get("calibration", {})
    if not isinstance(cal_raw, dict):
        raise ValueError("'vpn.calibration' must be an object")
    defaults = VPNCalibration()
    calibration = VPNCalibration(
        state_connect_offset_from_right=int(cal_raw.get(
            "state_connect_offset_from_right", defaults.state_connect_offset_from_right)),
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

    accounts: list[AccountEntry] = []
    for i, a in enumerate(raw.get("accounts", []), start=1):
        if not isinstance(a, dict):
            raise ValueError(f"accounts[{i}] must be an object with 'email' and 'password'")
        email_a = str(a.get("email", "")).strip()
        password_a = str(a.get("password", "")).strip()
        if not email_a or not password_a:
            raise ValueError(f"accounts[{i}] must have non-empty 'email' and 'password'")
        accounts.append(AccountEntry(email=email_a, password=password_a))

    return Config(
        repeat=repeat,
        vpn=vpn,
        cleanup=cleanup,
        tabs=tabs,
        check_downloads=check_downloads,
        clean_start=clean_start,
        cleanup_mode=cleanup_mode_raw,
        accounts=tuple(accounts),
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

    def append_list(self, key: str, item: Any) -> None:
        """Append to a list-valued state key, coercing to a list first.

        Robust against a hand-edited / legacy state file where the key is missing or
        holds a non-list value (e.g. {}), which would otherwise raise on .append.
        """
        cur = self.data.get(key)
        if not isinstance(cur, list):
            cur = []
            self.data[key] = cur
        cur.append(item)
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


_NORD_BOUNDED_HELPERS = r"""
on findNordLocationList(rootElem, maxDepth)
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
            set kids to {}
            try
                set kids to UI elements of elem
            end try
            -- Signature: the Country/location list is the only element with
            -- a large row count whose first row is the "Select a location"
            -- heading — true regardless of which sibling index it lives at.
            if (count of kids) ≥ 10 then
                try
                    if (name of (item 1 of kids) as text) contains "Select a location" then return elem
                end try
            end if
            if d < maxDepth then
                repeat with k in kids
                    set end of stack to {k, d + 1}
                end repeat
            end if
        end repeat
    end tell
    return missing value
end findNordLocationList

on findFirstByRole(rootElem, roleWanted, maxDepth)
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
            set kids to {}
            try
                set kids to UI elements of elem
            end try
            repeat with k in kids
                set rr to ""
                try
                    set rr to role of k as text
                end try
                if rr is roleWanted then return k
            end repeat
            if d < maxDepth then
                repeat with k in kids
                    set end of stack to {k, d + 1}
                end repeat
            end if
        end repeat
    end tell
    return missing value
end findFirstByRole

on findNordSearchResults(rootElem, maxDepth)
    -- The search-results pane. Signature: the container whose first row is a
    -- "Heading. Countries" / "Heading. Cities" static text (NordVPN labels every
    -- search-result section that way). Found by that label rather than a sibling
    -- index so it keeps working across app versions/window sizes.
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
            set kids to {}
            try
                set kids to UI elements of elem
            end try
            if (count of kids) ≥ 1 then
                try
                    if ((name of (item 1 of kids)) as text) starts with "Heading." then return elem
                end try
            end if
            if d < maxDepth then
                repeat with k in kids
                    set end of stack to {k, d + 1}
                end repeat
            end if
        end repeat
    end tell
    return missing value
end findNordSearchResults

on findWideButton(rootElem, minWidth, maxDepth)
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
            set kids to {}
            try
                set kids to UI elements of elem
            end try
            repeat with k in kids
                set rr to ""
                try
                    set rr to role of k as text
                end try
                if rr is "AXButton" then
                    set s to {0, 0}
                    try
                        set s to size of k
                    end try
                    if (item 1 of s) > minWidth then return k
                end if
            end repeat
            if d < maxDepth then
                repeat with k in kids
                    set end of stack to {k, d + 1}
                end repeat
            end if
        end repeat
    end tell
    return missing value
end findWideButton
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
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            # This venv's Python.framework has no system CA roots wired into
            # ssl.create_default_context() (a known python.org-installer gap —
            # "Install Certificates.command" was never run). Without certifi,
            # every ipinfo.io call fails CERTIFICATE_VERIFY_FAILED, which then
            # gets silently mislabeled "rate-limited" below and can make VPN
            # verification false-positive on tunnel+route alone.
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

        # Detect the VPN app up front so a missing/uninstalled app fails fast and
        # clearly here, before any discovery/connect work, rather than surfacing
        # as a confusing failure deeper in the flow.
        if not self._open_provider_app(vpn_cfg.app):
            raise AutomationError(
                f"{vpn_cfg.app} app not found or could not be launched. "
                f"Install {vpn_cfg.app} and sign in first."
            )
        self.logger.log(
            f"{vpn_cfg.app} detected — starting VPN stage for cycle {cycle}",
            step="06", status="vpn_app_detected", cycle=cycle,
        )

        servers = self._resolve_server_list(vpn_cfg)

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

        # Disconnect only when a tunnel is actually up, so a run that starts
        # disconnected goes straight to searching and connecting. The check is
        # a local interface read, so skipping costs nothing and saves the whole
        # find-the-Pause-button round trip.
        #
        # A disconnect failure/timeout here is non-fatal — disc's value is only ever
        # used for logging below, never treated as required for success — so
        # a raised AutomationError is caught the same way rather than aborting the
        # whole cycle over what is, at worst, a slightly stale pre-connect state.
        if self.net.active_tunnel_interfaces():
            try:
                disc = self._click_disconnect(vpn_cfg.app)
            except AutomationError as exc:
                disc = f"error:{exc}"
            self.logger.log(f"Pre-connect disconnect: {disc}", step="06", status=disc)
        else:
            self.logger.log(
                "Not connected — skipping disconnect, going straight to connect",
                step="06", status="already_disconnected",
            )
        ui_state = self._read_ui_connection_state(vpn_cfg.app)
        self.logger.log(f"{vpn_cfg.app} UI connection state: {ui_state}", step="06",
                        ui_connection_state=ui_state)

        # Re-capture baseline AFTER disconnect so baseline_ip reflects the bare (non-VPN) IP.
        # This handles the case where the VPN app auto-reconnects during the subsequent setup
        # AppleScript (which can take several seconds): the post-connect IP equals the
        # pre-disconnect VPN IP, making ip==baseline_ip (even though connection succeeded).
        # If the IP hasn't changed yet (auto-reconnect or slow teardown), disable the
        # ip-change guard by using None — country + tunnel-interface checks are sufficient.
        # The route baseline must be re-read here too. It was first captured
        # while the old tunnel was still up, so it held the VPN's own gateway
        # (10.5.0.2) — and every NordVPN server hands out that same gateway, so
        # the route-change check could never fire and each attempt burned its
        # full verify timeout ("route unchanged (gw=10.5.0.2 baseline=10.5.0.2)"
        # repeated until the server was abandoned). After the disconnect this
        # reads the real, non-VPN gateway, so a genuine change is detectable.
        post_disc_route = self.net.default_route_gateway()
        if post_disc_route and post_disc_route != baseline_route:
            self.logger.log(
                f"Post-disconnect baseline: route={post_disc_route} "
                f"(was {baseline_route} — the old tunnel's gateway)",
                step="06",
            )
            baseline_route = post_disc_route

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
        # Positions, not names: the rotation clicks the Nth row of the expanded
        # list, and `servers` supplies the name that row is expected to carry.
        indices_to_try = [(start_idx + off) % len(servers) for off in range(len(servers))]
        last_exc: AutomationError | None = None
        slot_baseline_ip = baseline_ip  # refreshed per attempt
        slot_baseline_route = baseline_route  # refreshed per attempt
        # Capture the VPN IP used in the previous *successful* cycle so we can skip states
        # that are in the same server cluster (same IP) and advance to a fresh one.
        prev_vpn_ip = self.state.data.get("last_vpn_ip")
        # Every individual VPN operation below already has its own bounded timeout;
        # this is an additional overall ceiling on the whole rotation loop so a
        # long server list where each attempt fails slowly (e.g. a mismatched AX
        # lookup on a different Mac) still gives up within a fixed, generous
        # bound instead of working through the entire list one slow failure at
        # a time. Does not affect the already-persisted rotation index.
        cycle_deadline = time.monotonic() + DEFAULT_VPN_CYCLE_BUDGET_SEC

        for attempt_i, target_index in enumerate(indices_to_try):
            target_server = servers[target_index]
            if time.monotonic() > cycle_deadline:
                last_exc = AutomationError(
                    f"VPN cycle budget ({DEFAULT_VPN_CYCLE_BUDGET_SEC}s) exceeded after "
                    f"{attempt_i}/{len(indices_to_try)} server(s) tried this cycle"
                )
                self.logger.log(
                    f"VPN cycle budget exceeded — stopping after {attempt_i} attempt(s)",
                    step="06", status="vpn_cycle_budget_exceeded", cycle=cycle,
                )
                break
            if attempt_i > 0:
                self.logger.log(
                    f"Server {servers[indices_to_try[attempt_i - 1]]} failed; retrying with {target_server}",
                    step="06", server=target_server,
                )
                if self.net.active_tunnel_interfaces():
                    try:
                        disc2 = self._click_disconnect(vpn_cfg.app)
                    except AutomationError as exc:
                        disc2 = f"error:{exc}"
                    self.logger.log(f"Retry pre-disconnect: {disc2}", step="06", status=disc2)
                else:
                    self.logger.log("Retry: not connected — skipping disconnect",
                                    step="06", status="already_disconnected")
                # _click_disconnect already waited for the tunnel to actually
                # go down, so the baseline below is read off a settled network
                # rather than after a guessed delay.
                snap2 = self.net.snapshot()
                slot_baseline_ip = snap2.get("public_ip")
                slot_baseline_route = self.net.default_route_gateway()
                self.logger.log(
                    f"Retry baseline: ip={slot_baseline_ip} route={slot_baseline_route}", step="06",
                )

            self.logger.log(
                f"Cycle {cycle} target {vpn_cfg.app} server: {target_server} "
                f"(list position {target_index + 1}/{len(servers)}, "
                f"attempt {attempt_i + 1})",
                step="06", cycle=cycle, target_server=target_server,
                server_index=target_index + 1,
            )
            self.logger.log(
                f"[NORDVPN] Server {target_index + 1}/{len(servers)}: {target_server}",
                step="06",
            )
            self.logger.log("[NORDVPN] Connecting...", step="06")

            # Any of this server's own AppleScript operations failing (a real
            # osascript error, not just a soft "server_not_found" status) must
            # only fail THIS attempt, not the whole cycle — same treatment
            # _poll_verify already gets below. Without this, one bad server
            # (or a transient AX hiccup) would abort the entire rotation
            # instead of falling through to the next slot.
            try:
                ui_status = self._click_server_at_index(
                    vpn_cfg.app, vpn_cfg.location, target_index, expected=target_server,
                )
            except AutomationError as exc:
                last_exc = exc
                self.logger.log(
                    f"{vpn_cfg.app} server '{target_server}': click failed: {exc}",
                    step="06", status="click_error", server=target_server,
                )
                continue
            self.logger.log(
                f"{vpn_cfg.app} server '{target_server}': {ui_status}",
                step="06", status=ui_status, server=target_server,
            )
            if ui_status not in (
                "server_clicked",
                "row_clicked",
                "connect_button_clicked",
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
                    self.logger.log("[NORDVPN] Connection verified.", step="06")
                    snap = self.net.snapshot()
                    new_ip = snap.get("public_ip")
                    # Skip this state if it landed on the same IP as the previous successful
                    # cycle — that means it's in the same server cluster.  Disconnect and
                    # advance to the next state rather than deliver a repeat address.
                    if (new_ip and prev_vpn_ip
                            and new_ip == prev_vpn_ip
                            and prev_vpn_ip != baseline_ip):
                        self.logger.log(
                            f"IP {new_ip} repeats previous cycle — "
                            f"disconnecting and skipping {target_server}",
                            step="06", server=target_server, status="ip_repeat_skip",
                        )
                        self._click_disconnect(vpn_cfg.app)
                        time.sleep(2.5)
                        last_exc = AutomationError(
                            f"IP {new_ip} repeated (prev={prev_vpn_ip}); "
                            f"skipping {target_server}"
                        )
                        continue
                    self._record_server(target_server)
                    # Advance the rotation past the server actually connected to,
                    # not merely past the one this cycle started at. The pointer
                    # is moved up front (so a crash mid-cycle still advances),
                    # but that up-front value assumes the first candidate wins.
                    # When a cycle falls through to a later server, leaving the
                    # pointer where it is makes the NEXT cycle start on the very
                    # server that just connected — the same city twice in a row.
                    rot[vpn_cfg.location] = (target_index + 1) % len(servers)
                    self.logger.log(
                        f"Rotation advanced past {target_server} "
                        f"(list position {target_index + 1}); "
                        f"next cycle starts at index {rot[vpn_cfg.location]} "
                        f"({servers[rot[vpn_cfg.location]]})",
                        step="06", rotation_index=rot[vpn_cfg.location],
                    )
                    # Persist the VPN IP so the next cycle can detect repeats.
                    if new_ip:
                        self.state.data["last_vpn_ip"] = new_ip
                    # Record full verified session (slot name, not assumed real server name)
                    sessions = self.state.data.setdefault("vpn_sessions", [])
                    sessions.append({
                        "cycle": cycle,
                        "slot": target_server,
                        "verified": True,
                        "utun": (snap.get("tunnel_interfaces") or [None])[0],
                        "public_ip": new_ip,
                        "country": snap.get("country"),
                        "verified_at": datetime.now().isoformat(),
                    })
                    self.state.save()
                self.logger.log("[NORDVPN] Starting existing workflow.", step="06")
                return result
            except AutomationError as exc:
                self.logger.log(
                    f"[NORDVPN] Connection timed out for {target_server}.", step="06",
                )
                last_exc = exc
                continue

        self.logger.log(
            f"VPN connect failed for cycle {cycle}: {last_exc or 'all slots exhausted'} — "
            f"rotation index unaffected, will retry next cycle per existing behavior",
            step="06", status="vpn_connect_failed", cycle=cycle,
        )
        raise last_exc or AutomationError("All VPN slots exhausted without a verified connection")

    @staticmethod
    def _provider_token(app_name: str) -> str:
        """Substring expected to appear in ipinfo.org for the provider."""
        n = app_name.lower()
        if "surfshark" in n:
            return "surfshark"
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

    def _ensure_frontmost(self, app_name: str, timeout: float = 3.0) -> bool:
        """Bring `app_name` to the front and wait until it reports frontmost.

        Every NordVPN interaction is a synthesized Quartz click at an
        AX-reported point, and macOS gives the first click on a background
        window to activating that window — the control under the pointer never
        sees it. So a click fired while NordVPN sits behind Chrome or Podcasts
        silently does nothing (verified: identical search, identical row
        coordinates, connects when frontmost, no-ops when not — the hover time
        before the click makes no difference either way).

        The VPN stage runs right after the browser/Podcasts work, so focus can
        easily have moved on; call this immediately before reading coordinates
        and clicking, not once at the start of a multi-step sequence.
        """
        # Resolved through the same candidate list every other script here uses,
        # so an app whose process name differs from its display name still works.
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
            if procName is "" then return "ERROR:vpn_process_not_found"
            tell process procName
                set frontmost to true
                delay 0.2
                return (frontmost as text)
            end tell
        end tell
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                if run_osascript(script, timeout=15, label=f"{app_name} frontmost") == "true":
                    return True
            except AutomationError:
                return False
            if time.monotonic() >= deadline:
                self.logger.log(
                    f"{app_name} would not come to the front — clicks may not register",
                    step="06", app=app_name,
                )
                return False

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

    # Fixed offset (dx, dy) from the "Pause connection" button's own AX-reported
    # top-left corner to the "Disconnect" item in the popover it opens. NordVPN's
    # SwiftUI popover is a genuine on-screen AXPopover but System Events cannot
    # enumerate its contents (confirmed: the popover shows up as a window-level
    # UI element with zero readable children), so — like the legacy ProtonVPN
    # connector `VPNCalibration` was built for — this one interaction needs a
    # calibrated pixel offset rather than an AX lookup. Empirically measured;
    # everything else in this connector is driven by live AX coordinates.
    _NORD_DISCONNECT_MENU_OFFSET = (53.0, 290.0)

    @staticmethod
    def _quartz_click(x: float, y: float) -> None:
        """Synthesize a real mouse click at a screen point via Quartz.

        NordVPN's custom SwiftUI controls (location rows, expand chevrons,
        the Pause/Disconnect popover) do not respond to AXPress — the `click`
        AppleScript/System Events command silently no-ops on them — so every
        interaction with them is driven by a genuine Quartz mouse-down/up
        event at the control's AX-reported position instead.
        """
        import Quartz  # type: ignore[import]

        def _mouse(kind: Any, px: float, py: float) -> None:
            pt = Quartz.CGPointMake(px, py)
            ev = Quartz.CGEventCreateMouseEvent(None, kind, pt, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

        _mouse(Quartz.kCGEventMouseMoved, x, y)
        time.sleep(0.08)
        _mouse(Quartz.kCGEventLeftMouseDown, x, y)
        time.sleep(0.05)
        _mouse(Quartz.kCGEventLeftMouseUp, x, y)

    def _nord_search_results_present(self, app_name: str) -> bool:
        """True while NordVPN's search-results pane is on screen.

        Clicking a city row connects and snaps the app back to the dashboard,
        so this pane disappearing is the observable confirmation that a row
        click actually landed rather than being swallowed by window activation.
        """
        probe = _NORD_BOUNDED_HELPERS + f"""
        tell application "System Events"
            tell process "{app_name.replace('"', '')}"
                if not (exists window 1) then return "gone"
                if (my findNordSearchResults(window 1, 10)) is missing value then return "gone"
                return "present"
            end tell
        end tell
        """
        try:
            return run_osascript(probe, timeout=20, label=f"{app_name} results present") == "present"
        except AutomationError:
            return False

    def _is_frontmost_app(self, app_name: str) -> bool:
        """True when `app_name` owns the frontmost window.

        Guards the typing step. `keystroke` goes to whichever app is frontmost,
        not to the process named in the tell block, so this is what proves the
        query will land in NordVPN's search box. It is checked after clicking
        the box: if that click actually landed on a window sitting on top of
        NordVPN, that other app becomes frontmost and is detected here.

        NordVPN's own AXFocused attribute is not usable for this — its SwiftUI
        search field reports AXFocused false even while it is accepting keys.
        """
        probe = f"""
        tell application "System Events"
            return name of first process whose frontmost is true
        end tell
        """
        try:
            return run_osascript(probe, timeout=20, label="frontmost app") == app_name
        except AutomationError:
            return False

    def _nordvpn_search(self, app_name: str, query: str, expect_name: str = "") -> str:
        """Type `query` into NordVPN's search field and wait (bounded) for the
        results for THIS query to render. Returns "ok" or an "ERROR:..." string.

        The field is focused with a real Quartz click and filled with real
        keystrokes: NordVPN's SwiftUI search binding does not fire when the AX
        value is set directly (verified — the text appears but no search runs).

        The settle poll waits for a row actually named `expect_name` (defaults to
        `query`) rather than just "some rows exist": the previous query's results
        stay on screen while the new search runs, so a generic "results present"
        check returns immediately against stale rows and the caller then searches
        the wrong list. Waiting for the specific expected row is both correct and
        faster than any fixed sleep — it returns the moment the match appears.
        """
        expect_name = expect_name or query
        process_list = self._process_name_candidates(app_name)
        locate = _NORD_BOUNDED_HELPERS + f"""
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
                delay 0.25
                if not (exists window 1) then return "ERROR:no_window"
                set tf to my findFirstByRole(window 1, "AXTextField", 10)
                if tf is missing value then return "ERROR:no_search_field"
                set p to position of tf
                set s to size of tf
                return ((item 1 of p) as text) & "," & ((item 2 of p) as text) & "," & ((item 1 of s) as text) & "," & ((item 2 of s) as text)
            end tell
        end tell
        """
        raw = run_osascript(locate, timeout=20, label=f"{app_name} find search field")
        if raw.startswith("ERROR:"):
            return raw
        try:
            x, y, w, h = (float(v) for v in raw.split(","))
        except ValueError:
            return f"ERROR:bad_search_field:{raw!r}"

        # Click the search box, then confirm it actually took focus before
        # typing. `keystroke` goes to whatever is frontmost, not to the process
        # named in the tell block, so typing without this check can spray the
        # query into another app's text field if NordVPN is not really in
        # front (observed: the query landed in an editor window instead).
        clicked = False
        for _ in range(2):
            if not self._ensure_frontmost(app_name):
                return "ERROR:app_not_frontmost"
            self._quartz_click(x + w / 2, y + h / 2)
            time.sleep(0.25)
            if self._is_frontmost_app(app_name):
                clicked = True
                break
        if not clicked:
            return "ERROR:search_box_click_hit_another_window"

        q_esc = query.replace("\\", "\\\\").replace('"', '\\"')
        typ = f"""
        tell application "System Events"
            tell process "{app_name.replace('"', '')}"
                keystroke "a" using command down
                delay 0.12
                keystroke "{q_esc}"
                return "ok"
            end tell
        end tell
        """
        try:
            run_osascript(typ, timeout=20, label=f"{app_name} type search")
        except AutomationError as exc:
            return f"ERROR:{exc}"

        # Bounded poll for THIS query's results, rather than a fixed sleep.
        expect_esc = expect_name.replace('"', '\\"')
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            time.sleep(0.3)
            probe = _NORD_BOUNDED_HELPERS + f"""
            tell application "System Events"
                tell process "{app_name.replace('"', '')}"
                    if not (exists window 1) then return "pending"
                    set res to my findNordSearchResults(window 1, 10)
                    if res is missing value then return "pending"
                    repeat with r in (UI elements of res)
                        set kids to {{}}
                        try
                            set kids to UI elements of r
                        end try
                        if (count of kids) ≥ 1 then
                            set nm to ""
                            try
                                set nm to (name of (item 1 of kids)) as text
                            end try
                            if nm is "{expect_esc}" then return "ready"
                        end if
                    end repeat
                    return "pending"
                end tell
            end tell
            """
            try:
                if run_osascript(probe, timeout=15, label=f"{app_name} search settle") == "ready":
                    return "ok"
            except AutomationError:
                continue
        return "ERROR:search_results_not_rendered"

    def _dismiss_blocking_dialog(self, app_name: str) -> bool:
        """Close a modal NordVPN puts over its main window, if one is up.

        The app shows subscription/upsell sheets ("Unlock NordVPN's advanced
        online protection", "Activate your subscription") as a second window
        covering the main one. While it is up there is no search box to click
        and no location list to read, so the whole flow stalls. Closing it is
        a no-op when no such window exists.

        Returns True if a dialog was closed.
        """
        script = f"""
        tell application "System Events"
            tell process "{app_name.replace('"', '')}"
                if (count of windows) < 2 then return "none"
                repeat with w in windows
                    -- The main window carries the search box; anything else
                    -- covering it is the sheet to close.
                    if (count of (text fields of w)) is 0 then
                        try
                            click (first button of w whose subrole is "AXCloseButton")
                            return "closed"
                        end try
                    end if
                end repeat
                return "none"
            end tell
        end tell
        """
        try:
            if run_osascript(script, timeout=20, label=f"{app_name} dismiss dialog") == "closed":
                self.logger.log(
                    f"Closed a blocking {app_name} dialog covering the main window",
                    step="06", app=app_name,
                )
                time.sleep(0.4)
                return True
        except AutomationError:
            pass
        return False

    def _nordvpn_open_country_list(self, app_name: str, location: str) -> str:
        """Open `location`'s server list in the NordVPN window, exactly as a
        person would: click the search box, type the location, wait for its
        result to appear, click the arrow next to it, and wait for the server
        rows to become visible.

        Returns "ok" or an "ERROR:..." string. Does not read the rows — both
        callers need the list on screen, but only discovery needs its names,
        and enumerating every row costs far more than the connect path can
        afford to spend each cycle.
        """
        self._dismiss_blocking_dialog(app_name)
        # Retry the search once: right after a connect or disconnect the app is
        # still settling, and the results for this query can miss the settle
        # poll's window even though the search box accepted the text. Observed
        # costing two whole rotation slots to ERROR:search_results_not_rendered
        # before the third attempt went through.
        status = self._nordvpn_search(app_name, location)
        if status != "ok":
            self.logger.log(
                f"{location} results did not render ({status}) — searching again",
                step="06", location=location,
            )
            self._dismiss_blocking_dialog(app_name)
            status = self._nordvpn_search(app_name, location)
        if status != "ok":
            return status

        process_list = self._process_name_candidates(app_name)
        loc_esc = location.replace('"', '\\"')
        expand = _NORD_BOUNDED_HELPERS + f"""
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
                if not (exists window 1) then return "ERROR:no_window"
                set res to my findNordSearchResults(window 1, 10)
                if res is missing value then return "ERROR:no_results"
                set rws to UI elements of res
                repeat with i from 1 to (count of rws)
                    set r to item i of rws
                    set kids to {{}}
                    try
                        set kids to UI elements of r
                    end try
                    -- A country result row is [name, "N cities", expand chevron].
                    if (count of kids) is 3 then
                        set nm to ""
                        try
                            set nm to (name of (item 1 of kids)) as text
                        end try
                        if nm is "{loc_esc}" then
                            -- Already expanded if the next row is a city/Fastest row.
                            set needsExpand to true
                            if (i + 1) ≤ (count of rws) then
                                set nk to {{}}
                                try
                                    set nk to UI elements of (item (i + 1) of rws)
                                end try
                                if (count of nk) < 3 then set needsExpand to false
                            end if
                            if needsExpand then
                                set chev to item 3 of kids
                                set p to position of chev
                                set s to size of chev
                                return "EXPAND|" & ((item 1 of p) as text) & "," & ((item 2 of p) as text) & "," & ((item 1 of s) as text) & "," & ((item 2 of s) as text)
                            end if
                            return "READY|" & i
                        end if
                    end if
                end repeat
                return "ERROR:country_not_in_results"
            end tell
        end tell
        """
        raw = run_osascript(expand, timeout=25, label=f"{app_name} expand {location}")
        if raw.startswith("ERROR:"):
            return raw
        if raw.startswith("EXPAND|"):
            try:
                x, y, w, h = (float(v) for v in raw[len("EXPAND|"):].split(","))
            except ValueError:
                return f"ERROR:bad_expand:{raw!r}"
            # Same activation rule as the row click: a chevron click only
            # registers while NordVPN is frontmost, and the settle poll above
            # can have run for seconds since the search brought it forward.
            self._ensure_frontmost(app_name)
            self._quartz_click(x + w / 2, y + h / 2)

            # Wait for the server list to actually become visible rather than
            # guessing at a delay: poll until the first city row exists, and
            # re-click the arrow once if the expansion never happened. A fixed
            # sleep here was the cause of intermittent "no_cities_in_results"
            # — the rows had not rendered yet when the enumeration ran.
            if not self._wait_for_city_rows(app_name):
                self._ensure_frontmost(app_name)
                self._quartz_click(x + w / 2, y + h / 2)
                if not self._wait_for_city_rows(app_name):
                    return "ERROR:list_did_not_expand"
        return "ok"

    def _nordvpn_read_server_rows(self, app_name: str, location: str) -> dict[str, Any]:
        """Read the server rows currently shown under the expanded `location`,
        in the order NordVPN displays them.

        City rows are the ones carrying exactly one child (their name). The
        country header row has three (name, "N cities", the expand arrow) and
        the "Fastest server" shortcut has two, so both are skipped — the
        rotation never lands on NordVPN's own auto-pick.

        Returns {"cities": [...]} or {"error": "..."}.
        """
        open_status = self._nordvpn_open_country_list(app_name, location)
        if open_status != "ok":
            return {"error": open_status}

        process_list = self._process_name_candidates(app_name)
        enumerate_rows = _NORD_BOUNDED_HELPERS + f"""
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
                if not (exists window 1) then return "ERROR:no_window"
                set res to my findNordSearchResults(window 1, 10)
                if res is missing value then return "ERROR:no_results"
                set rws to UI elements of res
                set out to ""
                repeat with i from 1 to (count of rws)
                    set kids to {{}}
                    try
                        set kids to UI elements of (item i of rws)
                    end try
                    -- City rows carry exactly one child (their name). The country
                    -- header has 3 and the "Fastest server" shortcut has 2, so
                    -- both are skipped by this test — the auto-pick entry never
                    -- enters the rotation.
                    if (count of kids) is 1 then
                        set nm to ""
                        try
                            set nm to (name of (item 1 of kids)) as text
                        end try
                        if nm is not "" then set out to out & nm & "⁣"
                    end if
                end repeat
                return "OK|" & out
            end tell
        end tell
        """
        raw2 = run_osascript(enumerate_rows, timeout=30, label=f"{app_name} list {location} cities")
        if raw2.startswith("ERROR:"):
            return {"error": raw2}
        _, names = raw2.split("|", 1)
        seen: set[str] = set()
        cities: list[str] = []
        for c in names.split("⁣"):
            c = c.strip()
            if c and c not in seen:
                seen.add(c)
                cities.append(c)
        if not cities:
            return {"error": "ERROR:no_cities_in_results"}
        return {"cities": cities}

    def _resolve_server_list(self, vpn_cfg: VPNConfig) -> list[str]:
        """The rotation's server list for `vpn_cfg.location`, in GUI order.

            explicit override > remembered GUI list > fresh GUI discovery

        The list always comes from what NordVPN's own window actually shows —
        search the country, click its expand arrow, read the rows that appear
        (`_discover_servers`). Never from a CLI, an HTTP API, or a hardcoded
        set of names, and never NordVPN's own auto-pick: the "Fastest server"
        shortcut row is filtered out during discovery so the rotation always
        knows exactly which server it asked for.

        Once discovered, the list is remembered in state and reused for every
        later cycle — the expensive search-and-expand only runs again when
        there is nothing usable to reuse (first run, or the previous attempt
        came back truncated/invalid).
        """
        if vpn_cfg.servers:
            return list(vpn_cfg.servers)

        location = vpn_cfg.location
        discovered = self.state.data.setdefault("discovered_servers_by_location", {})

        # A single-server list means the previous discovery was incomplete
        # (NordVPN's US list has many cities); with only 1 server the rotation
        # index never advances and the same IP is used on every cycle. Treat it
        # as invalid and re-read the list from the GUI.
        cached = list(discovered.get(location, []))
        if cached and len(cached) > 1:
            self.logger.log(
                f"Reusing the {location} server list already read from the "
                f"{vpn_cfg.app} window ({len(cached)} servers)",
                step="06", location=location, source="cache",
            )
            return cached
        if cached:
            self.logger.log(
                f"Remembered {location} list has only {len(cached)} server — "
                f"invalid, re-reading it from the {vpn_cfg.app} window",
                step="06", location=location,
            )

        # The app is already open — connect_with_config launches it before
        # calling here — so this goes straight to search → expand → read rows.
        self.logger.log(
            f"Reading the {location} server list from the {vpn_cfg.app} window",
            step="06", location=location, app=vpn_cfg.app,
        )
        servers = self._discover_servers(location, vpn_cfg.location_code, vpn_cfg.app)
        if not servers:
            raise AutomationError(
                f"No servers discovered for {location} in {vpn_cfg.app}. "
                f"Open {vpn_cfg.app}, search '{location}', expand the country, "
                f"and try again — OR set vpn.servers explicitly in input/tasks.json."
            )

        self.logger.log(
            f"Discovered {len(servers)} servers for {location}: {servers[:5]}"
            + ("..." if len(servers) > 5 else ""),
            step="06", location=location, server_count=len(servers),
        )
        discovered[location] = servers
        self.state.save()
        return servers

    def _discover_servers(
        self, location: str, location_code: str, app_name: str = "NordVPN"
    ) -> list[str]:
        """Read the servers NordVPN actually shows for `location`, in GUI order.

        Performs the human flow through `_nordvpn_read_server_rows`:
        click the search box, type the location, wait for its result, click the
        expand arrow, then read the rows that appear (e.g. "Atlanta",
        "Chicago", "New York", ...). Whatever the window shows is what the
        rotation uses — the names and the count come from the GUI, never from
        a fixed list, and NordVPN's "Fastest server" shortcut is excluded so
        the app never picks a server on its own.

        Returns [] if the list could not be read, which the caller turns into
        a clear error rather than a substitute list.
        """
        self.logger.log(f"[NORDVPN] Searching for {location} servers...", step="06", location=location)

        result = self._nordvpn_read_server_rows(app_name, location)
        if "error" in result:
            self.logger.log(
                f"Could not read the {location} server list from {app_name}: "
                f"{result['error']}",
                step="06", location=location,
            )
            self.logger.log(f"[NORDVPN] Failed to retrieve {location} server list.", step="06")
            return []

        # Defensive de-dup, preserving GUI order — NordVPN's own city list
        # shouldn't contain duplicates, but the rotation math (index advances
        # by exactly one per cycle, wraps via modulo) silently breaks if it
        # ever did, so this is cheap insurance rather than a sign duplicates
        # are expected.
        seen: set[str] = set()
        cities: list[str] = []
        for c in result["cities"]:
            if c not in seen:
                seen.add(c)
                cities.append(c)

        self.logger.log(
            f"Discovered {len(cities)} NordVPN servers for {location}: "
            f"{cities[:5]}" + ("..." if len(cities) > 5 else ""),
            step="06", location=location, server_count=len(cities), source="gui",
        )
        self.logger.log(f"[NORDVPN] Found {len(cities)} {location} servers.", step="06")
        return cities

    def _wait_for_city_rows(self, app_name: str, timeout: float = 5.0) -> bool:
        """Poll until the expanded country shows at least one city row.

        Used straight after clicking the expand arrow: SwiftUI renders the
        revealed rows asynchronously, so the list is read only once it is
        genuinely visible instead of after a guessed delay.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._nord_city_row_at(app_name, 0).startswith("OK|"):
                return True
            time.sleep(0.2)
        return False

    def _nord_results_pane_bounds(self, app_name: str) -> tuple[float, float, float, float] | None:
        """Screen bounds (x, y, width, height) of the search-results pane."""
        probe = _NORD_BOUNDED_HELPERS + f"""
        tell application "System Events"
            tell process "{app_name.replace('"', '')}"
                if not (exists window 1) then return "ERROR:no_window"
                set res to my findNordSearchResults(window 1, 10)
                if res is missing value then return "ERROR:no_results"
                set p to position of res
                set sz to size of res
                return ((item 1 of p) as text) & "," & ((item 2 of p) as text) & "," & ((item 1 of sz) as text) & "," & ((item 2 of sz) as text)
            end tell
        end tell
        """
        try:
            raw = run_osascript(probe, timeout=20, label=f"{app_name} pane bounds")
        except AutomationError:
            return None
        if raw.startswith("ERROR:"):
            return None
        try:
            x, y, w, h = (float(v) for v in raw.split(","))
        except ValueError:
            return None
        return x, y, w, h

    @staticmethod
    def _quartz_scroll(x: float, y: float, dy: float) -> None:
        """Scroll by `dy` pixels with the pointer over (x, y).

        Positive `dy` moves the content down (reveals earlier rows), negative
        moves it up (reveals later rows) — the same direction convention as a
        physical wheel.
        """
        import Quartz  # type: ignore[import]

        move = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventMouseMoved, Quartz.CGPointMake(x, y),
            Quartz.kCGMouseButtonLeft,
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
        time.sleep(0.05)
        ev = Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitPixel, 1, int(dy)
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

    def _scroll_row_into_view(self, app_name: str, index: int) -> str:
        """Scroll the expanded list until row `index` is inside the visible
        pane, then return its "OK|<name>|x,y,w,h".

        NordVPN shows about ten of a country's rows at a time — the United
        States list alone has ~55 — so every row past the first screenful has
        AX coordinates far below the window. Clicking those directly lands
        outside the app entirely, which is why the row has to be brought into
        view first, exactly as a person would scroll to it.

        Returns the row string, or an "ERROR:..." string.
        """
        bounds = self._nord_results_pane_bounds(app_name)
        if bounds is None:
            return "ERROR:no_results_pane"
        px, py, pw, ph = bounds
        # Keep the target off the very edges: a row flush against the top or
        # bottom of the scroll area can be clipped or overlapped by the pane's
        # own fade/heading.
        margin = 60.0
        centre_x, centre_y = px + pw / 2, py + ph / 2

        row = self._nord_city_row_at(app_name, index)
        previous_y: float | None = None
        for _ in range(8):
            if row.startswith("ERROR:"):
                return row
            try:
                _, _name, geo = row.split("|", 2)
                x, y, w, h = (float(v) for v in geo.split(","))
            except ValueError:
                return f"ERROR:bad_row:{row!r}"

            if py + margin <= y <= py + ph - margin - h:
                return row
            # The list has hit its top or bottom stop — the last row can never
            # reach the middle of the pane — so settle for genuinely visible
            # rather than comfortably centred.
            if previous_y is not None and abs(y - previous_y) < 1.0:
                if py <= y <= py + ph - h:
                    return row
                return "ERROR:row_would_not_scroll_into_view"
            previous_y = y

            # A pixel scroll event moves this list 1:1 (verified: three -200px
            # events moved a row from y=356 to y=-244), so aiming straight at
            # the middle of the pane lands in one step. The position is still
            # re-read rather than assumed, since the list can hit its top or
            # bottom stop partway.
            self._quartz_scroll(centre_x, centre_y, (py + ph / 2) - y)
            time.sleep(0.15)
            row = self._nord_city_row_at(app_name, index)
        return "ERROR:row_would_not_scroll_into_view"

    def _nord_city_row_at(self, app_name: str, index: int) -> str:
        """Locate the `index`-th (0-based) city row of the currently expanded
        country in NordVPN's search results.

        Returns "OK|<name>|x,y,w,h" or an "ERROR:..." string. An error here just
        means the expanded list is not on screen — every successful connect
        snaps the app back to the dashboard, so the caller re-runs the
        search-and-expand and asks again.

        City rows are the ones carrying exactly one child (their name). The
        country header row has three (name, "N cities", the expand arrow) and
        the "Fastest server" shortcut has two, so both are skipped — the
        rotation only ever lands on a real, individually chosen server and
        never on NordVPN's own auto-pick.
        """
        probe = _NORD_BOUNDED_HELPERS + f"""
        tell application "System Events"
            tell process "{app_name.replace('"', '')}"
                if not (exists window 1) then return "ERROR:no_window"
                set res to my findNordSearchResults(window 1, 10)
                if res is missing value then return "ERROR:list_not_expanded"
                set rws to UI elements of res
                set n to 0
                repeat with r in rws
                    set kids to {{}}
                    try
                        set kids to UI elements of r
                    end try
                    if (count of kids) is 1 then
                        set nm to ""
                        try
                            set nm to (name of (item 1 of kids)) as text
                        end try
                        if nm is not "" then
                            if n is {int(index)} then
                                set p to position of r
                                set sz to size of r
                                return "OK|" & nm & "|" & ((item 1 of p) as text) & "," & ((item 2 of p) as text) & "," & ((item 1 of sz) as text) & "," & ((item 2 of sz) as text)
                            end if
                            set n to n + 1
                        end if
                    end if
                end repeat
                return "ERROR:row_index_out_of_range"
            end tell
        end tell
        """
        try:
            return run_osascript(probe, timeout=20, label=f"{app_name} row {index}")
        except AutomationError as exc:
            return f"ERROR:{exc}"

    def _click_server_at_index(
        self, app_name: str, location: str, index: int, expected: str = ""
    ) -> str:
        """Connect by clicking the `index`-th server row of `location`'s
        expanded list in the NordVPN window — the only way this script selects
        a server.

        The full human flow is: click the search box, type the location, wait
        for its result, click the expand arrow, wait for the rows, click the
        one for this cycle (`_nordvpn_open_country_list` performs the
        first four steps). That expansion is skipped whenever the list is
        already on screen from a previous step, and re-run only when it is
        gone — which is what happens after each successful connect, since the
        app returns to its dashboard.

        Returns "connect_button_clicked" or a short failure status.
        """
        row = self._scroll_row_into_view(app_name, index)
        if row.startswith("ERROR:"):
            # List not on screen (or its rows are no longer valid) — reopen it:
            # search box → type location → await result → click expand arrow.
            self.logger.log(
                f"{location} list not on screen ({row[len('ERROR:'):]}) — "
                f"reopening it in {app_name}",
                step="06", location=location,
            )
            opened = self._nordvpn_open_country_list(app_name, location)
            if opened != "ok":
                self.logger.log(
                    f"Could not expand {location} in {app_name}: {opened}",
                    step="06", location=location,
                )
                return "server_not_found"
            row = self._scroll_row_into_view(app_name, index)
            if row.startswith("ERROR:"):
                self.logger.log(
                    f"{location} expanded but row {index + 1} is unavailable: {row}",
                    step="06", location=location,
                )
                return "server_not_found"

        try:
            _, name, coords = row.split("|", 2)
            x, y, w, h = (float(v) for v in coords.split(","))
        except ValueError:
            return f"bad_row:{row!r}"

        if expected and name != expected:
            # The visible list changed under us (NordVPN added/removed a city).
            # Report it and click what is actually at this position — the
            # rotation walks positions, and the caller records the real name.
            self.logger.log(
                f"Server row {index + 1} is now '{name}', not '{expected}' — "
                f"the {location} list has changed",
                step="06", location=location, server=name,
            )

        # Two attempts: macOS gives the first click on a background window to
        # activating it, so a click fired while NordVPN sits behind Chrome or
        # Podcasts never reaches the row (see _ensure_frontmost).
        for attempt in range(2):
            self._ensure_frontmost(app_name)
            self._quartz_click(x + w / 2, y + h / 2)
            # Poll for the results pane to clear — that is the app confirming
            # the row was activated, and it happens long before the tunnel is
            # up. The connection itself is verified separately by _poll_verify.
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                time.sleep(0.25)
                if not self._nord_search_results_present(app_name):
                    self.logger.log(
                        f"NordVPN: clicked server {index + 1} — '{name}'",
                        step="06", server=name, server_index=index + 1,
                    )
                    return "connect_button_clicked"
            if attempt == 0:
                self.logger.log(
                    f"Click on '{name}' did not register — re-focusing and retrying",
                    step="06", server=name,
                )
                fresh = self._scroll_row_into_view(app_name, index)
                if fresh.startswith("OK|"):
                    try:
                        _, name, coords = fresh.split("|", 2)
                        x, y, w, h = (float(v) for v in coords.split(","))
                    except ValueError:
                        pass
        return "server_not_found"

    def _record_ip(self, ip: str | None) -> None:
        if not ip:
            return
        self.state.data["last_public_ip"] = ip
        if ip not in self.state.data["used_public_ips"]:
            self.state.data["used_public_ips"].append(ip)
        self.state.save()

    def _click_disconnect(self, app_name: str) -> str:
        """Disconnect NordVPN's active connection.

        NordVPN's Dashboard shows a "Pause connection" button (~260x48, role
        AXButton) only while connected or connecting; disconnected, that
        space instead holds a "Secure my connection" control (role
        AXUnknown, not AXButton) — so the Pause button is found by scanning
        for the first sufficiently-wide AXButton, and its absence means
        there's nothing to disconnect. Clicking it opens a popover (Pause for
        5/15/30 min, 1/24 hours, Disconnect); the "Disconnect" item is then
        reached via `_NORD_DISCONNECT_MENU_OFFSET` (see its docstring for why).

        A discovery pass (`_discover_servers`) leaves the app parked on a
        location list rather than the Dashboard — and `connect_with_config`
        calls this right after discovery — so this first checks for that
        screen (by its "Select a location" heading, via a bounded structural
        search — see `_NORD_BOUNDED_HELPERS`) and, if found, clicks the back
        arrow to return to the Dashboard before looking for Pause connection.
        The Pause button is likewise found by a bounded whole-window search
        rather than a fixed sibling index, since NordVPN's exact element
        layout is not guaranteed identical across app versions, window sizes,
        or macOS versions on a different Mac. Also dismisses a blocking
        "Activate your subscription" dialog first, if present: a different
        Mac/account can be signed in with an expired trial, which NordVPN
        shows as a modal that pushes every element deeper in the AX tree.
        """
        process_list = self._process_name_candidates(app_name)
        script = _BOUNDED_HELPERS + _NORD_BOUNDED_HELPERS + f"""
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
                delay 0.3
                if not (exists window 1) then return "no_window"

                -- Check for the list FIRST: the common repeated case is
                -- already being on that screen, and searching for a
                -- "Close" button that isn't there would otherwise walk the
                -- whole already-expanded list needlessly first.
                set lg to my findNordLocationList(window 1, 10)
                if lg is missing value then
                    -- Connecting via the search field leaves the app on the
                    -- search-results screen, where the Dashboard's Pause button
                    -- doesn't exist. Treat that the same as the browse screen:
                    -- go back to the Dashboard first, otherwise this reports
                    -- "no_disconnect_button", the tunnel stays up, and the NEXT
                    -- cycle's city click is ignored by NordVPN (it only honours
                    -- a new location once the current one is disconnected).
                    set lg to my findNordSearchResults(window 1, 10)
                end if
                if lg is not missing value then
                    try
                        set mg to item 1 of (UI elements of window 1)
                        set backBtn to item 2 of (UI elements of mg)
                        set p to position of backBtn
                        set s to size of backBtn
                        return "BACK|" & (item 1 of p) & "," & (item 2 of p) & "," & (item 1 of s) & "," & (item 2 of s)
                    end try
                end if

                try
                    set closeBtn to my findButtonByName(window 1, "Close", 10)
                    if closeBtn is not missing value then
                        click closeBtn
                        delay 0.5
                    end if
                end try

                set pauseBtn to my findWideButton(window 1, 150, 10)
                if pauseBtn is not missing value then
                    set p to position of pauseBtn
                    set s to size of pauseBtn
                    return "PAUSE|" & (item 1 of p) & "," & (item 2 of p) & "," & (item 1 of s) & "," & (item 2 of s)
                end if

                return "no_disconnect_button"
            end tell
        end tell
        """
        raw = run_osascript(script, timeout=20, label=f"{app_name} disconnect")
        if raw.startswith("BACK|"):
            try:
                x, y, w, h = (float(v) for v in raw[len("BACK|"):].split(","))
            except ValueError:
                return f"bad_response:{raw!r}"
            self._quartz_click(x + w / 2, y + h / 2)
            time.sleep(1.0)
            raw = run_osascript(script, timeout=20, label=f"{app_name} disconnect (post-back)")
        if not raw.startswith("PAUSE|"):
            return raw
        try:
            x, y, w, h = (float(v) for v in raw[len("PAUSE|"):].split(","))
        except ValueError:
            return f"bad_response:{raw!r}"
        # Confirm the tunnel actually went down instead of assuming the click
        # worked. Reporting success too early makes connect_with_config capture
        # its "post-disconnect" baseline while still on the old tunnel, and the
        # verifier then compares the new connection's route against the old
        # VPN's own gateway — which can never differ, so the attempt burns its
        # whole verify timeout before falling through to the next server.
        dx, dy = self._NORD_DISCONNECT_MENU_OFFSET
        for attempt in range(2):
            self._ensure_frontmost(app_name)
            self._quartz_click(x + w / 2, y + h / 2)   # open the Pause popover
            time.sleep(0.5)
            self._quartz_click(x + dx, y + dy)         # its "Disconnect" item
            if self._wait_for_tunnel_down():
                return "disconnect_clicked"
            if attempt == 0:
                self.logger.log(
                    f"{app_name} still has a tunnel after Disconnect — retrying once",
                    step="06", app=app_name,
                )
                raw = run_osascript(script, timeout=20, label=f"{app_name} disconnect retry")
                if not raw.startswith("PAUSE|"):
                    break
                try:
                    x, y, w, h = (float(v) for v in raw[len("PAUSE|"):].split(","))
                except ValueError:
                    break
        return "disconnect_not_confirmed"

    def _wait_for_tunnel_down(self, timeout: float = 8.0) -> bool:
        """Poll until no VPN tunnel interface remains, up to `timeout`.

        Reads local interfaces only (ifconfig), so this costs nothing and needs
        no network — it is a real teardown check, not a guessed delay.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.net.active_tunnel_interfaces():
                return True
            time.sleep(0.3)
        return False

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
            # (a VPN kill-switch drops the default route while the tunnel is active).
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

    @staticmethod
    def _norm_show(name: str) -> str:
        """Lowercase + strip for tolerant show-name comparison."""
        return (name or "").strip().lower()

    # Content area starts past the ~180px sidebar; text left of this is chrome/sidebar.
    _CONTENT_X = 200

    def _content_show_title(self, nodes: "list | None" = None) -> str:
        """The show title as read from the content-area AX text (topmost heading).

        The Podcasts window has no AX title on current macOS builds (System Events
        returns `missing value`), so this reads the show name straight from the page
        content instead — the AXStaticText title node sits at the top of the content
        column.  Returns '' if the page hasn't rendered a title yet.
        """
        nodes = nodes if nodes is not None else self._ax_nodes()
        best: "tuple[int, str] | None" = None
        for role, t, x, y, w, h in nodes:
            if (role in ("AXStaticText", "AXHeading") and t and x >= self._CONTENT_X
                    and y > 60 and len(t.strip()) >= 2):
                if best is None or y < best[0]:
                    best = (y, t.strip())
        return best[1] if best else ""

    def _content_matches(self, nodes: "list", want: str) -> bool:
        """True if the requested show name appears in the content-area text/buttons."""
        if not want:
            return False
        for role, t, x, y, w, h in nodes:
            if not t or x < self._CONTENT_X:
                continue
            if role not in ("AXStaticText", "AXButton", "AXHeading"):
                continue
            nt = self._norm_show(t)
            if not nt:
                continue
            # Exact, or a substring match guarded by length so short generic words
            # ("the", "a") can't false-match.
            if nt == want or (len(nt) >= 4 and (want in nt or nt in want)):
                return True
        return False

    def wait_for_show_loaded(self, expected_name: str, timeout_sec: int = 20) -> str:
        """Block until Podcasts has actually navigated to the requested show.

        `wait_for_window` only proves *a* window exists (always true after the first
        tab), so on later tabs the app could still be displaying the PREVIOUS show
        when Follow/download run — leaving the new show unfollowed and its episodes
        never queued.  This reads the visible show name from the content-area AX text
        (the window has no AX title on current builds) and returns once it matches
        `expected_name` (derived from the Chrome tab title).

        Returns 'loaded' when the name matches, 'loaded:changed' when the displayed
        show changed to a different real show with a rendered page (covers name-format
        differences), 'loaded:unverified' if a show page rendered but nothing matched
        within the timeout, or 'timeout' if no show page appeared at all.
        """
        want = self._norm_show(expected_name)
        # Title shown when we start = the PREVIOUS show on tab 2+; a change away from
        # it (with a rendered page) means navigation happened.
        initial = self._norm_show(self._content_show_title())
        deadline = time.time() + timeout_sec
        saw_show_page = False
        while time.time() < deadline:
            nodes = self._ax_nodes()
            # A "What's New"/subscription "Continue" modal (common on a cold launch or
            # right after sign-in) COVERS the show page, so the show never renders and
            # this gate would otherwise time out.  Dismiss it from the nodes we already
            # have — clicking its Continue button lets the page through.
            self._dismiss_continue_in_nodes(nodes)
            has_page = self._follow_button_state(nodes) in ("not_followed", "following")
            if has_page:
                saw_show_page = True
            # Strongest signal: the requested show name is on the page.
            if self._content_matches(nodes, want):
                return "loaded"
            # Weaker signal: the page's title changed to a different real show.
            cur = self._norm_show(self._content_show_title(nodes))
            if has_page and cur and initial and cur != initial:
                return "loaded:changed"
            time.sleep(0.6)
        return "loaded:unverified" if saw_show_page else "timeout"

    def capture_show_name(self) -> str:
        """Read the podcast show name from the current Podcasts window.

        Tries the content-area AX title first (the window has no AX title on current
        macOS builds — System Events returns `missing value`), then the window title,
        then the first prominent heading in the AX tree.
        Returns a non-empty string or 'unknown_show'.
        """
        native = self._content_show_title()
        if native and native.lower() not in ("podcasts", "apple podcasts", "missing value"):
            return native

        script = """
        tell application "System Events"
            tell process "Podcasts"
                if not (exists window 1) then return "unknown_show"
                -- Window title often contains the show name (e.g. "My Podcast – Podcasts")
                set wTitle to ""
                try
                    set wTitle to name of window 1 as string
                end try
                if wTitle is not "" and wTitle is not "Podcasts" and wTitle is not "missing value" then
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
            # Priority 1: the real "See All" button that loads the full episode list.
            # It lives in the content area (x > content_left) and has exact text "See All"
            # or "View All". The macOS menu bar's "Show All" is excluded by the x filter.
            see_all_cands = sorted(
                ((x + w // 2, y + h // 2) for role, t, x, y, w, h in nodes
                 if role in ("AXButton", "AXLink") and x > content_left and w > 0
                 and t.strip() in ("See All", "View All", "All Episodes")),
                key=lambda c: c[1],
            )
            # Priority 2: "Episodes" tab/radio-button — only use when there is no
            # dedicated "See All" (some shows render the full list behind a tab).
            episodes_tab_cands = sorted(
                ((x + w // 2, y + h // 2) for role, t, x, y, w, h in nodes
                 if role in ("AXRadioButton", "AXTab", "AXButton", "AXCell") and w > 0
                 and t.startswith(("Episodes", "All Episodes"))
                 and not t.strip().startswith("All Episodes")),   # avoid dup with p1
                key=lambda c: c[1],
            )
            cands = see_all_cands or episodes_tab_cands
            if cands:
                before = _episode_count(nodes)
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
            if t is "See All" then return true
            if t is "View All" then return true
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
        # Fixed settle after every download click so the app registers it before we
        # move on to the next episode/tab.
        time.sleep(3)

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

    def navigate_to_recently_updated_tab(self) -> str:
        """Navigate to the Recently Updated section in the Podcasts sidebar.

        Mirrors navigate_to_downloaded_tab, targeting the 'Recently Updated' sidebar
        item instead of 'Downloaded'.
        """
        try:
            import Quartz  # type: ignore[import]
        except ImportError:
            return "quartz_unavailable"

        try:
            run_osascript('tell application "Podcasts" to activate',
                          timeout=5, label="activate before Recently Updated nav")
        except AutomationError:
            pass
        time.sleep(0.3)

        nodes = self._ax_nodes()
        cx = cy = 0
        for role, t, x, y, w, h in nodes:
            if t == "Recently Updated" and x < 400 and w > 100 and h > 0:
                cx = x + w // 2
                cy = y + h // 2
                break

        if not cx:
            self.logger.log("navigate_to_recently_updated_tab: ERROR:not_found", step="14")
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

        self.logger.log(f"Clicked Recently Updated sidebar at ({cx},{cy})", step="14")
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

    def _find_downloaded_card_frame_native_only(self) -> tuple[int, int, int, int] | None:
        """Same card search as _find_downloaded_card_frame, but WITHOUT the System
        Events fallback (~15-30s) when nothing is found — just the ~1s native walk.

        Used by the Recently Updated cleanup's own polling/verification steps
        (_wait_card_gone, _recently_updated_confirmed_empty), which call this
        repeatedly and don't need the authoritative-but-slow fallback on every
        call: cleanup_all_from_recently_updated's main loop already does one full
        (fallback-included) _find_downloaded_card_frame() call per iteration, so
        that's where the "is a card genuinely there" authority lives.
        """
        nodes = self._ax_nodes()
        win_x = win_y = win_w = win_h = 0
        for role, _t, x, y, w, h in nodes:
            if role == "AXWindow" and w > 400 and h > 400:
                win_x, win_y, win_w, win_h = x, y, w, h
                break
        if win_w == 0:
            return None
        content_left = win_x + 240
        content_top = win_y + 60
        win_right = win_x + win_w
        win_bottom = win_y + win_h
        cards = [
            (x, y, w, h) for role, _t, x, y, w, h in nodes
            if x > content_left and y > content_top
            and x < win_right and y < win_bottom
            and 80 <= w <= 800 and 80 <= h <= 900
            and h > w
            and h < w * 4
            and w < win_w - 100
        ]
        if not cards:
            return None
        cards.sort(key=lambda c: (c[1], c[0]))
        return cards[0]

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

    def _click_unfollow_show_menu_item_ax(self) -> bool:
        """Click 'Unfollow Show' in the card context menu via AX walk + Quartz.

        Retries up to 3 times with 0.5s gaps — the Mac Catalyst context menu can
        take slightly longer than the nominal 1.2s wait to appear in the AX tree.
        """
        try:
            import Quartz  # type: ignore[import]
        except ImportError:
            return False

        for attempt in range(3):
            nodes = self._ax_nodes()
            for role, text, x, y, w, h in nodes:
                if role == "AXButton" and h > 0 and h < 40 and text == "Unfollow Show":
                    cx, cy = x + w // 2, y + h // 2
                    pt = Quartz.CGPoint(x=float(cx), y=float(cy))
                    for kind in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
                        ev = Quartz.CGEventCreateMouseEvent(None, kind, pt, Quartz.kCGMouseButtonLeft)
                        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                        time.sleep(0.05)
                    self.logger.log(f"Clicked 'Unfollow Show' at ({cx},{cy})", step="14")
                    return True
            if attempt < 2:
                time.sleep(0.5)
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

    def _count_downloaded_cards(self) -> int:
        """Fast native count of show cards on the Downloaded grid.

        Uses the same window + card geometry as _find_downloaded_card_frame's native
        path. Used to detect that an (async) removal has actually taken effect.
        """
        nodes = self._ax_nodes()
        win_x = win_y = win_w = win_h = 0
        for role, _t, x, y, w, h in nodes:
            if role == "AXWindow" and w > 400 and h > 400:
                win_x, win_y, win_w, win_h = x, y, w, h
                break
        if win_w == 0:
            return 0
        content_left = win_x + 240
        content_top = win_y + 60
        win_right = win_x + win_w
        win_bottom = win_y + win_h
        return sum(
            1 for role, _t, x, y, w, h in nodes
            if x > content_left and y > content_top
            and x < win_right and y < win_bottom
            and 80 <= w <= 800 and 80 <= h <= 900
            and h > w and h < w * 4 and w < win_w - 100
        )

    def _wait_downloaded_cards_below(self, previous: int, timeout: int = 12) -> bool:
        """Poll until the Downloaded card count drops below `previous` (the removal
        actually completed) or the grid empties. Returns True once it drops.

        Removals are asynchronous and some shows take longer than others; this waits
        for the just-triggered removal to take effect before the next card is scanned,
        so a slow removal is never skipped.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1.0)
            now = self._count_downloaded_cards()
            if now == 0 or now < previous:
                return True
        return False

    def _downloaded_grid_confirmed_empty(self, checks: int = 2, delay: float = 1.2) -> bool:
        """Re-confirm the grid is empty across a few native re-scans.

        Reached only after the authoritative _find_downloaded_card_frame already
        returned None; the extra native re-checks make the "no shows remain" decision
        resilient to a slow async removal that might still be settling a card in.
        """
        for _ in range(checks):
            time.sleep(delay)
            self.navigate_to_downloaded_tab()
            if self._has_back_button():
                self._click_back_button()
                time.sleep(0.5)
            if self._count_downloaded_cards() > 0:
                return False
        return True

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

        `expected_cards`: advisory only (kept for signature compatibility). Cleanup no
        longer stops on a count — it removes cards and re-scans until the Downloaded
        grid is verified empty, waiting for each (async) removal to actually complete
        first, so a slow removal can never leave a show behind.
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

        # Ensure the Podcasts window is large enough for the card grid to render.
        # Cards require at least ~700px height; a tiny window (e.g. 250px) hides them.
        try:
            run_osascript(
                """
tell application "System Events"
    tell process "Podcasts"
        if (count of windows) > 0 then
            set w to window 1
            set sz to size of w
            set ww to item 1 of sz
            set wh to item 2 of sz
            if ww < 900 or wh < 700 then
                set size of w to {900, 700}
                delay 0.3
            end if
        end if
    end tell
end tell
""",
                timeout=8,
                label="cleanup_resize_window",
            )
        except Exception:
            pass

        results: list[dict[str, Any]] = []
        removed = 0
        for iteration in range(50):
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
                # No card visible — but a previous removal may still be settling, so
                # confirm the grid is really empty across a few re-checks before
                # stopping. Only a persistently empty grid ends cleanup.
                if self._downloaded_grid_confirmed_empty():
                    self.logger.log(
                        f"Downloads cleanup: grid confirmed empty after "
                        f"{iteration} removal(s)",
                        step="14",
                    )
                    results.append({"iteration": iteration + 1, "result": "done"})
                    break
                self.logger.log(
                    "Downloads cleanup: a card resurfaced after settle — continuing",
                    step="14",
                )
                continue

            card_x, card_y, card_w, card_h = frame
            # Card count before this removal — used afterwards to confirm the (async)
            # removal actually took effect before we scan for the next card.
            cards_before = self._count_downloaded_cards()
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

            # AX click on 'Unfollow Show' menu item via ApplicationServices + Quartz.
            ax_ok = self._click_unfollow_show_menu_item_ax()
            confirm = "no_sheet"
            actual_removed = False

            if not ax_ok:
                # Keyboard fallback: 'Unfollow Show' is first item → Down×1 + Enter
                self.logger.log(
                    f"Downloads cleanup card {iteration + 1}: Unfollow Show not found via AX — keyboard fallback",
                    step="14",
                )
                _key(0x7D, True); _key(0x7D, False)
                time.sleep(0.3)
                _key(0x24, True); _key(0x24, False)
                time.sleep(0.8)

            time.sleep(0.4)
            confirm = self._click_confirmation_remove()
            actual_removed = True

            result_label = "unfollowed:ax" if ax_ok else "unfollowed:keyboard"
            if confirm not in ("no_sheet",):
                result_label += f"+confirmed:{confirm}"

            self.logger.log(
                f"Downloads cleanup card {iteration + 1}: {result_label}", step="14"
            )
            results.append({"iteration": iteration + 1, "result": result_label})
            if actual_removed:
                removed += 1
            # Wait for this removal to ACTUALLY take effect before scanning again.
            # Removals are async and some shows take longer, so proceeding immediately
            # can skip a still-removing card and leave the last show behind.
            if self._wait_downloaded_cards_below(cards_before, timeout=12):
                self.logger.log(
                    f"Downloads cleanup card {iteration + 1}: removal confirmed "
                    f"(cards were {cards_before})", step="14",
                )
            else:
                self.logger.log(
                    f"Downloads cleanup card {iteration + 1}: card count did not drop "
                    f"below {cards_before} within timeout — re-scanning anyway", step="14",
                )

        return results

    def _wait_card_gone(self, prev_x: int, prev_y: int, timeout: int = 5) -> bool:
        """Poll until the card previously at (prev_x, prev_y) is no longer the
        top-left card — i.e. it was actually removed from the grid (or a different
        show now occupies that slot). Used to verify a Recently Updated removal
        completed before moving on to the next show.

        Uses the native-only card search (~1s) rather than the full
        _find_downloaded_card_frame (which falls back to a ~15s System Events scan
        on every "no card" result) — this is just a settle-time poll, not the
        authoritative empty check, so a fast/cheap read is enough here.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1.0)
            frame = self._find_downloaded_card_frame_native_only()
            if frame is None:
                return True
            fx, fy, _fw, _fh = frame
            if abs(fx - prev_x) > 10 or abs(fy - prev_y) > 10:
                return True
        return False

    def _recently_updated_confirmed_empty(self, checks: int = 1, delay: float = 1.2) -> bool:
        """Re-confirm the Recently Updated grid is empty with one quick re-scan.

        Reached only after the caller's own _find_downloaded_card_frame() call
        (the slow, authoritative one, with the System Events fallback) already
        returned None. This re-check uses the fast native-only search — the
        authority that a card is really gone was already established by the
        caller; this just re-samples once after a short settle to catch a slow
        async unfollow. If a card resurfaces anyway, the outer
        cleanup_all_from_recently_updated loop catches it on its next iteration.
        """
        for _ in range(checks):
            time.sleep(delay)
            self.navigate_to_recently_updated_tab()
            if self._has_back_button():
                self._click_back_button()
                time.sleep(0.5)
            if self._find_downloaded_card_frame_native_only() is not None:
                return False
        return True

    def cleanup_all_from_recently_updated(self) -> list[dict[str, Any]]:
        """Remove every show from the Recently Updated section: Remove Download,
        then Unfollow Show — leaving the Downloaded section untouched.

        The Recently Updated section displays show cards the same way the Downloads
        tab does. Strategy: navigate to Recently Updated, then loop:
          1. Find the first show card via _find_downloaded_card_frame (BFS, card geometry).
          2. Hover the card center to reveal the ⋯ button, click it, click 'Remove Download'.
          3. Wait 4s for the removal to complete.
          4. Re-open the ⋯ menu on the same card and click 'Unfollow Show' — this is
             what actually drops the card from Recently Updated (a list of followed
             shows), since Remove Download alone only clears the local download.
          5. Verify the card is actually gone before moving to the next one.
          6. Re-navigate to Recently Updated and repeat until the section is empty.
        """
        try:
            import Quartz  # type: ignore[import]
        except ImportError:
            return [{"iteration": 0, "result": "quartz_unavailable"}]

        def _warp(x: int, y: int) -> None:
            pt_w = Quartz.CGPoint(x=float(x), y=float(y))
            Quartz.CGWarpMouseCursorPosition(pt_w)
            mv = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventMouseMoved, pt_w, Quartz.kCGMouseButtonLeft
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, mv)
            time.sleep(0.05)

        def _open_three_dots_menu(three_x: int, three_y: int, artwork_cx: int, artwork_cy: int) -> None:
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

        results: list[dict[str, Any]] = []
        for iteration in range(50):
            # Re-navigate each iteration — card removal may shift view focus.
            nav = self.navigate_to_recently_updated_tab()
            if nav != "navigated":
                self.logger.log(f"Recently Updated cleanup: nav failed ({nav})", step="14")
                results.append({"iteration": iteration + 1, "result": f"nav_failed:{nav}"})
                break

            # One authoritative find per iteration (native walk + System Events
            # fallback if needed) — the "card may still be rendering" / "settling"
            # retry is handled below by _recently_updated_confirmed_empty's fast
            # native-only re-check instead of a second slow authoritative call here.
            frame = self._find_downloaded_card_frame()
            if frame is None:
                # No card found — determine whether the section is empty or we landed
                # on a show's episode page instead of the grid. The episode page has a
                # "Back" button in the nav bar; the grid does not.
                if self._has_back_button():
                    self.logger.log(
                        "Recently Updated cleanup: on show page — clicking Back to reach grid",
                        step="14",
                    )
                    self._click_back_button()
                    time.sleep(0.8)
                    frame = self._find_downloaded_card_frame_native_only()
            if frame is None:
                # No card visible — but a previous unfollow may still be settling, so
                # confirm the section is really empty across a few re-checks before
                # stopping. Only a persistently empty section ends cleanup.
                if self._recently_updated_confirmed_empty():
                    self.logger.log(
                        f"Recently Updated cleanup: confirmed empty after "
                        f"{iteration} removal(s)",
                        step="14",
                    )
                    results.append({"iteration": iteration + 1, "result": "done"})
                    break
                self.logger.log(
                    "Recently Updated cleanup: a card resurfaced after settle — continuing",
                    step="14",
                )
                continue

            card_x, card_y, card_w, card_h = frame
            # Artwork on these grid cards is always a square whose side equals the
            # card width. The ⋯ button appears at the lower-right of the artwork
            # square (not the lower-right of the full card, which includes the title
            # strip below the artwork).
            artwork_h = card_w
            three_x = card_x + card_w - 20       # 20 px inside right edge of artwork
            three_y = card_y + artwork_h - 20     # 20 px above bottom of artwork square
            artwork_cx = card_x + card_w // 2
            artwork_cy = card_y + artwork_h // 2

            self.logger.log(
                f"Recently Updated cleanup card {iteration + 1}: "
                f"({card_x},{card_y},{card_w},{card_h}) "
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

            # Step 1: three-dot menu → Remove Download.
            _open_three_dots_menu(three_x, three_y, artwork_cx, artwork_cy)
            remove_ok = self._click_remove_menu_item_ax()
            if remove_ok:
                self._click_confirmation_remove(max_attempts=5)
            else:
                self.logger.log(
                    f"Recently Updated cleanup card {iteration + 1}: "
                    "Remove Download item not found via AX — continuing to Unfollow",
                    step="14",
                )

            # Step 2: wait for the removal to complete before continuing.
            time.sleep(4)

            # Step 3: re-open the three-dot menu on the same card → Unfollow Show.
            _open_three_dots_menu(three_x, three_y, artwork_cx, artwork_cy)
            unfollow_ok = self._click_unfollow_show_menu_item_ax()
            confirm = "no_sheet"
            if unfollow_ok:
                confirm = self._click_confirmation_remove(max_attempts=5)
            else:
                self.logger.log(
                    f"Recently Updated cleanup card {iteration + 1}: "
                    "Unfollow Show item not found via AX",
                    step="14",
                )

            # Step 4: verify this show actually left Recently Updated before moving on.
            settled = self._wait_card_gone(card_x, card_y, timeout=5)

            result_label = "removed:unfollowed" if unfollow_ok else "unfollow_failed"
            if not remove_ok:
                result_label += ":remove_download_not_found"
            if confirm not in ("no_sheet",):
                result_label += f"+confirmed:{confirm}"
            result_label += ":verified" if settled else ":unverified"

            self.logger.log(
                f"Recently Updated cleanup card {iteration + 1}: {result_label}", step="14"
            )
            results.append({"iteration": iteration + 1, "result": result_label})

        return results

    @staticmethod
    def _as_str(value: str) -> str:
        """Escape a Python string for safe embedding in an AppleScript string literal."""
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def check_sign_in_state(self) -> str:
        """Return 'signed_in', 'signed_out', or 'unknown'."""
        script = """
tell application "System Events"
    tell process "Podcasts"
        try
            set acctItem to menu bar item "Account" of menu bar 1
            click acctItem
            delay 0.5
            set mItems to {}
            try
                set mItems to name of every menu item of menu 1 of acctItem
            end try
            key code 53
            delay 0.3
            repeat with n in mItems
                if n contains "Sign In" then return "signed_out"
            end repeat
            return "signed_in"
        on error e
            return "unknown"
        end try
    end tell
end tell
"""
        try:
            return run_osascript(script, timeout=15, label="check_sign_in_state").strip()
        except Exception:
            return "unknown"

    def get_signed_in_email(self) -> str:
        """Return the email shown in the Podcasts Account menu, or '' if not signed in."""
        script = """
tell application "System Events"
    tell process "Podcasts"
        try
            set acctItem to menu bar item "Account" of menu bar 1
            click acctItem
            delay 0.5
            set mItems to name of every menu item of menu 1 of acctItem
            key code 53
            delay 0.2
            repeat with i from 1 to (count of mItems)
                set thisItem to (item i of mItems) as string
                if thisItem contains "@" then return thisItem
            end repeat
            return ""
        on error e
            return ""
        end try
    end tell
end tell
"""
        try:
            return run_osascript(script, timeout=12, label="get_signed_in_email").strip()
        except Exception:
            return ""

    def _click_button_in_podcasts_windows(self, button_name: str) -> str:
        """Click a named button anywhere in Podcasts windows or sheets."""
        btn = self._as_str(button_name)
        script = """
tell application "System Events"
    tell process "Podcasts"
        try
            repeat with w in windows
                try
                    if exists button "__BTN__" of w then
                        click button "__BTN__" of w
                        return "clicked"
                    end if
                end try
                try
                    repeat with s in sheets of w
                        if exists button "__BTN__" of s then
                            click button "__BTN__" of s
                            return "clicked_in_sheet"
                        end if
                    end repeat
                end try
            end repeat
            return "not_found"
        on error e
            return "error:" & e
        end try
    end tell
end tell
""".replace("__BTN__", btn)
        try:
            return run_osascript(script, timeout=10, label=f"click_button_{button_name}").strip()
        except Exception as exc:
            return f"error:{exc}"

    def _wait_and_click_button(self, button_name: str, timeout: int = 30) -> str:
        """Poll until a named button appears in Podcasts windows/sheets, then click it."""
        btn = self._as_str(button_name)
        script = """
tell application "System Events"
    tell process "Podcasts"
        set deadline to (current date) + __TIMEOUT__
        repeat while (current date) < deadline
            try
                repeat with w in windows
                    try
                        if exists button "__BTN__" of w then
                            click button "__BTN__" of w
                            return "clicked"
                        end if
                    end try
                    try
                        repeat with s in sheets of w
                            if exists button "__BTN__" of s then
                                click button "__BTN__" of s
                                return "clicked_in_sheet"
                            end if
                        end repeat
                    end try
                end repeat
            end try
            delay 0.5
        end repeat
        return "timeout"
    end tell
end tell
""".replace("__BTN__", btn).replace("__TIMEOUT__", str(timeout))
        try:
            return run_osascript(script, timeout=timeout + 10, label=f"wait_click_{button_name}").strip()
        except Exception as exc:
            return f"error:{exc}"

    def _wait_for_sign_in_complete(self, timeout: int = 60) -> str:
        """Poll Account menu until it shows user info (not Sign In). Returns 'signed_in' or 'timeout'."""
        script = """
tell application "System Events"
    tell process "Podcasts"
        set deadline to (current date) + __TIMEOUT__
        repeat while (current date) < deadline
            try
                set acctItem to menu bar item "Account" of menu bar 1
                click acctItem
                delay 0.5
                set mItems to {}
                try
                    set mItems to name of every menu item of menu 1 of acctItem
                end try
                key code 53
                delay 0.3
                set hasSignIn to false
                repeat with n in mItems
                    if n contains "Sign In" then
                        set hasSignIn to true
                        exit repeat
                    end if
                end repeat
                if not hasSignIn and (count of mItems) > 0 then return "signed_in"
            end try
            delay 2
        end repeat
        return "timeout"
    end tell
end tell
""".replace("__TIMEOUT__", str(timeout))
        try:
            return run_osascript(script, timeout=timeout + 15, label="wait_for_sign_in_complete").strip()
        except Exception as exc:
            return f"error:{exc}"

    def _ax_click_button(self, button_text: "str | list[str]", deadline_sec: int = 8) -> str:
        """Find a button by text via native AX walk and Quartz-click it.

        Matches on AXButton whose first non-empty attribute (description/value/title)
        starts with button_text — handles both title-only and description-only buttons.
        Only considers on-screen buttons (x >= 0, y >= 0).

        button_text may be a single string or a list of candidate labels; the first
        matching label wins.  Matching is CASE-INSENSITIVE, because the same dialog
        button renders with different capitalization across macOS/Podcasts builds
        (e.g. "Other Options" vs "Other options", "Do Not Upgrade" vs "Do not
        upgrade") — a case-sensitive match silently failed on some Macs.
        """
        try:
            import Quartz  # type: ignore[import]
        except ImportError:
            return "error:quartz_unavailable"

        candidates = [button_text] if isinstance(button_text, str) else list(button_text)
        lowered = [c.lower() for c in candidates]

        deadline = time.time() + deadline_sec
        while time.time() < deadline:
            nodes = self._ax_nodes()
            for role, t, x, y, w, h in nodes:
                if not (role == "AXButton" and x >= 0 and y >= 0 and w > 0 and h > 0):
                    continue
                t_low = t.lower()
                if not any(t_low.startswith(c) for c in lowered):
                    continue
                cx, cy = x + w // 2, y + h // 2
                for k in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
                    ev = Quartz.CGEventCreateMouseEvent(
                        None, k, Quartz.CGPointMake(cx, cy),
                        Quartz.kCGMouseButtonLeft)
                    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                    time.sleep(0.06)
                self.logger.log(
                    f"AX button {candidates!r}: clicked {t!r} at ({cx},{cy})",
                    step="SIGNIN",
                )
                return "clicked"
            time.sleep(0.3)
        return f"not_found:{candidates}"

    def _ax_click_and_paste_in_textfield(
        self, field_index: int = 0, deadline_sec: int = 15, label: str = "field"
    ) -> str:
        """Find the Nth AXTextField in the Podcasts sign-in sheet via the native AX walk,
        Quartz-click it, select-all, then paste the clipboard contents.

        AppleScript's `text fields of sheet` only surfaces DIRECT children; the Podcasts
        sign-in sheet wraps the field in groups, so the AS count is always 0.  The native
        walk visits every node regardless of depth and finds the field reliably.
        """
        try:
            import Quartz  # type: ignore[import]
        except ImportError:
            return "error:quartz_unavailable"

        deadline = time.time() + deadline_sec
        while time.time() < deadline:
            nodes = self._ax_nodes()
            # Collect AXTextField nodes inside the sheet y-range (y>200) that are
            # on-screen (x>=0) — excludes the sidebar search box (y≈82) and the
            # off-screen search field (x=-1, y≈745).
            fields = [
                (x + w // 2, y + h // 2)
                for role, _t, x, y, w, h in nodes
                if role == "AXTextField" and y > 200 and x >= 0 and w > 50 and h > 10
            ]
            # Negative field_index means "from the end" — e.g. -1 = last field.
            # The password screen adds a second field (email locked + password active),
            # so field_index=-1 always picks the correct active field regardless of how
            # many fields are visible.
            idx = (len(fields) + field_index) if field_index < 0 else field_index
            if 0 <= idx < len(fields):
                cx, cy = fields[idx]
                # Click to focus the field
                for k in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
                    ev = Quartz.CGEventCreateMouseEvent(
                        None, k, Quartz.CGPointMake(cx, cy), Quartz.kCGMouseButtonLeft)
                    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                    time.sleep(0.05)
                time.sleep(0.2)
                # Select all + paste
                for key, mods in ((0, Quartz.kCGEventFlagMaskCommand),    # Cmd+A
                                  (9, Quartz.kCGEventFlagMaskCommand)):    # Cmd+V
                    dn = Quartz.CGEventCreateKeyboardEvent(None, key, True)
                    up = Quartz.CGEventCreateKeyboardEvent(None, key, False)
                    Quartz.CGEventSetFlags(dn, mods)
                    Quartz.CGEventSetFlags(up, mods)
                    Quartz.CGEventPost(Quartz.kCGHIDEventTap, dn)
                    time.sleep(0.05)
                    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
                    time.sleep(0.05)
                self.logger.log(
                    f"AX textfield {label}: clicked at ({cx},{cy}) and pasted",
                    step="SIGNIN",
                )
                return f"{label}_entered"
            time.sleep(0.4)
        return f"{label}_field_not_found"

    def sign_in_to_podcasts(self, email: str, password: str) -> str:
        """Full sign-in flow for Apple Podcasts. Returns 'signed_in', 'already_signed_in_correct', or error string."""
        self.activate()
        time.sleep(0.5)

        state = self.check_sign_in_state()
        self.logger.log(f"Pre-sign-in state: {state}", step="SIGNIN")
        if state == "signed_in":
            current_email = self.get_signed_in_email()
            self.logger.log(f"Currently signed in as: {current_email}", step="SIGNIN")
            if email.lower() in current_email.lower() or current_email.lower() in email.lower():
                self.logger.log(f"Already signed in with correct account ({email}) — skipping", step="SIGNIN")
                return "already_signed_in_correct"
            self.logger.log(f"Wrong account ({current_email} vs {email}); signing out", step="SIGNIN")
            signout_result = self.sign_out_of_podcasts()
            self.logger.log(f"Sign-out result: {signout_result}", step="SIGNIN")
            deadline = time.time() + 20
            while time.time() < deadline:
                time.sleep(2.5)
                post_state = self.check_sign_in_state()
                self.logger.log(f"Post-signout state: {post_state}", step="SIGNIN")
                if post_state != "signed_in":
                    break
            time.sleep(1.0)

        # Step 1: Account menu → Sign In…
        script = """
tell application "System Events"
    tell process "Podcasts"
        try
            set acctItem to menu bar item "Account" of menu bar 1
            click acctItem
            delay 0.4
            set clicked to false
            repeat with mi in menu items of menu 1 of acctItem
                if name of mi contains "Sign In" then
                    click mi
                    set clicked to true
                    exit repeat
                end if
            end repeat
            if clicked then
                return "opened"
            else
                key code 53
                return "sign_in_item_not_found"
            end if
        on error e
            return "error:" & e
        end try
    end tell
end tell
"""
        result = "sign_in_item_not_found"
        for attempt in range(1, 4):
            result = run_osascript(script, timeout=15, label="open_sign_in_dialog")
            self.logger.log(f"Open sign-in dialog (attempt {attempt}): {result}", step="SIGNIN")
            if "error" not in result and "not_found" not in result:
                break
            if attempt < 3:
                time.sleep(2.5)
        if "error" in result or "not_found" in result:
            return f"sign_in_dialog_failed:{result}"
        time.sleep(2.0)

        # Step 2: Enter email.
        # AppleScript's `text fields of window/sheet` only returns DIRECT children —
        # the Podcasts sign-in sheet nests the field inside groups, so the count is
        # always 0.  Use the native AX walk instead: find the topmost AXTextField
        # that is inside the sheet's y-range, click it with Quartz, then paste via
        # pbcopy+Cmd+V so special characters arrive safely.
        subprocess.run(["pbcopy"], input=email.encode("utf-8"), check=True)
        result = self._ax_click_and_paste_in_textfield(
            field_index=0, deadline_sec=15, label="email"
        )
        self.logger.log(f"Email entry: {result}", step="SIGNIN")
        if "not_found" in result:
            return f"email_field_not_found:{result}"
        time.sleep(0.5)

        # Step 3: Click Sign In (submits email) via AX walk + Quartz to avoid
        # AppleScript `button "X" of sheet` failing when the button title is empty.
        result = self._ax_click_button("Sign In", deadline_sec=8)
        self.logger.log(f"Sign In (email) button: {result}", step="SIGNIN")
        time.sleep(2.5)

        # Step 4: Enter password — paste via clipboard (safe for special characters).
        # After email submit the sheet has TWO fields: [email(locked), password(active)].
        # Use field_index=-1 (last field) so we always land on the password field whether
        # the form has one field or two.
        subprocess.run(["pbcopy"], input=password.encode("utf-8"), check=True)
        result = self._ax_click_and_paste_in_textfield(
            field_index=-1, deadline_sec=15, label="password"
        )
        self.logger.log(f"Password entry: {result}", step="SIGNIN")
        if "not_found" in result:
            return f"password_field_not_found:{result}"
        time.sleep(0.5)

        # Step 5: Click Sign In (submits password)
        result = self._ax_click_button("Sign In", deadline_sec=8)
        self.logger.log(f"Sign In (password) button: {result}", step="SIGNIN")
        time.sleep(3.0)

        # Step 6: Wait for "Other Options" button and click it (Apple Account Security dialog).
        # Pass capitalization variants — different macOS/Podcasts builds render the label
        # in title case or sentence case, and matching is now case-insensitive anyway.
        self.logger.log("Waiting for 'Other Options' dialog...", step="SIGNIN")
        result = self._ax_click_button(
            ["Other Options", "Other options"], deadline_sec=30)
        self.logger.log(f"Other options: {result}", step="SIGNIN")
        if "clicked" in result:
            time.sleep(2.0)

        # Step 7: Wait for "Do Not Upgrade" button and click it
        self.logger.log("Waiting for 'Do Not Upgrade' dialog...", step="SIGNIN")
        result = self._ax_click_button(
            ["Do Not Upgrade", "Do not upgrade", "Don't Upgrade", "Don’t Upgrade"],
            deadline_sec=20)
        self.logger.log(f"Do not upgrade: {result}", step="SIGNIN")
        if "clicked" in result:
            time.sleep(2.0)

        # Step 8: Wait until Account menu confirms sign-in
        self.logger.log("Waiting for Podcasts sign-in to complete...", step="SIGNIN")
        completion = self._wait_for_sign_in_complete(timeout=60)
        self.logger.log(f"Sign-in completion: {completion}", step="SIGNIN")

        return "signed_in" if completion == "signed_in" else f"sign_in_{completion}"

    # Button labels (lowercased) that mean the show is NOT yet followed.
    _FOLLOW_LABELS = ("follow", "+ follow", "＋ follow")

    def _find_follow_button(self, nodes: "list | None" = None) -> "tuple[int, int] | None":
        """Center of the Follow button if the show is currently NOT followed, else None."""
        nodes = nodes if nodes is not None else self._ax_nodes()
        for role, text, x, y, w, h in nodes:
            if role == "AXButton" and text and text.strip().lower() in self._FOLLOW_LABELS:
                return x + w // 2, y + h // 2
        return None

    def _follow_button_state(self, nodes: "list | None" = None) -> str:
        """Inspect the current show page and report follow control state.

        Returns 'not_followed' when a Follow button is present, else 'absent'.
        NOTE: on current macOS Podcasts the Follow button simply DISAPPEARS once the
        show is followed (it is replaced by Download / More — there is no persistent
        'Following'/'Unfollow' text button), so 'absent' on a rendered show page means
        'already followed'.  Callers distinguish the two via the page-title check.
        """
        return "not_followed" if self._find_follow_button(nodes) else "absent"

    def _dismiss_continue_in_nodes(self, nodes: "list") -> bool:
        """If a 'Continue' button is in the given AX snapshot, Quartz-click it.

        Used by the navigation gate to clear a blocking What's New/subscription modal
        without a second AX walk.  Returns True if it clicked.
        """
        try:
            import Quartz  # type: ignore[import]
        except ImportError:
            return False
        for role, t, x, y, w, h in nodes:
            if role == "AXButton" and t and t.strip().lower() == "continue" and w > 0 and h > 0:
                cx, cy = x + w // 2, y + h // 2
                for k in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
                    ev = Quartz.CGEventCreateMouseEvent(
                        None, k, Quartz.CGPointMake(cx, cy), Quartz.kCGMouseButtonLeft)
                    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                    time.sleep(0.05)
                self.logger.log(f"Nav gate: dismissed Continue modal at ({cx},{cy})", step="10")
                return True
        return False

    def click_follow_button(self) -> str:
        """Follow the current show, VERIFYING the Follow button disappears.

        The old version clicked the first Follow button and immediately returned
        'followed' with no verification — a click that didn't register (e.g. because
        the "What's New"/subscription modal was covering the button, or the page was
        still settling after navigating from a previous show) was silently reported as
        success, so one show in a multi-show run was left unfollowed.  This dismisses
        any blocking modal, clicks Follow, then confirms the Follow button is gone
        (the followed state on this build), retrying a few times.

        Returns 'followed_verified', 'already_following', or 'follow_unconfirmed'
        (non-fatal — the caller logs/records it).
        """
        try:
            import Quartz  # type: ignore[import]
        except ImportError:
            return "error:quartz_unavailable"

        def _click(cx: int, cy: int) -> None:
            pt = Quartz.CGPointMake(cx, cy)
            for kind in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
                ev = Quartz.CGEventCreateMouseEvent(None, kind, pt, Quartz.kCGMouseButtonLeft)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                time.sleep(0.05)

        time.sleep(1.5)
        for attempt in range(6):
            # A "What's New"/subscription modal can surface at any time and COVER the
            # Follow button — dismiss it before every attempt so our click lands on it.
            self.dismiss_continue_popup(deadline_sec=2)
            nodes = self._ax_nodes()
            follow_pt = self._find_follow_button(nodes)

            if follow_pt is None:
                # No Follow button.  If the show page has rendered (title present), the
                # button is gone because the show is already followed.  Otherwise the
                # header just hasn't rendered yet — wait and retry.
                if self._content_show_title(nodes):
                    return "already_following" if attempt == 0 else "followed_verified"
                time.sleep(1.5)
                continue

            # Follow button present — click it, then confirm it disappeared.
            _click(*follow_pt)
            # Fixed settle after every Follow click so the app registers it.
            time.sleep(3)
            self.dismiss_continue_popup(deadline_sec=2)
            if self._find_follow_button() is None:
                return "followed_verified"
            # Click didn't take (modal, or landed off-target) — loop and retry.

        return "follow_unconfirmed"

    def dismiss_continue_popup(self, deadline_sec: int = 5) -> str:
        """Dismiss the "What's New in Apple Podcasts" onboarding modal by clicking
        its bottom-center 'Continue' button.

        This modal appears after signing in with a fresh account, once the show
        page renders — its exact timing varies (right after sign-in, or later
        during render), and it BLOCKS the app until dismissed, breaking Follow
        and the download/cleanup steps that follow.  Because it persists once
        shown, this is called at the points where it blocks (after sign-in and
        before See All); whichever call sees it first clicks Continue and the
        rest become no-ops.  The Continue button's accessible name is exactly
        'Continue'; _ax_click_button matches it and clicks its center, which
        lands on the bottom-center button.  Non-fatal — a short poll that
        returns without error when the modal isn't present.
        """
        result = self._ax_click_button("Continue", deadline_sec=deadline_sec)
        if result == "clicked":
            self.logger.log("What's New popup: clicked Continue button", step="10")
        else:
            self.logger.log("What's New popup: none present", step="10")
        return result

    def sign_out_of_podcasts(self) -> str:
        """Sign out via Account → View Apple Account → System Settings Sign Out button."""
        step1 = """
tell application "Podcasts" to activate
delay 1.5
tell application "System Events"
    tell process "Podcasts"
        try
            set acctItem to menu bar item "Account" of menu bar 1
            click acctItem
            delay 0.5
            set found to false
            set alreadyOut to false
            repeat with mi in menu items of menu 1 of acctItem
                if name of mi contains "View Apple Account" then
                    click mi
                    set found to true
                    exit repeat
                end if
                if name of mi contains "Sign Out" then
                    click mi
                    set found to true
                    exit repeat
                end if
                if name of mi contains "Sign In" then
                    set alreadyOut to true
                    exit repeat
                end if
            end repeat
            if alreadyOut then
                key code 53
                return "already_signed_out"
            end if
            if not found then
                key code 53
                return "no_account_option"
            end if
            return "opened"
        on error e
            return "error:" & e
        end try
    end tell
end tell
"""
        try:
            r = run_osascript(step1, timeout=16, label="signout_open_settings").strip()
        except Exception as exc:
            return f"error:{exc}"
        if "error" in r or r in ("no_account_option", "already_signed_out"):
            return r
        time.sleep(3.5)

        try:
            import Quartz  # type: ignore[import]
        except ImportError:
            return "error:quartz_unavailable"

        try:
            run_osascript('tell application "System Events" to key code 53', timeout=5,
                          label="signout_escape")
        except Exception:
            pass
        time.sleep(0.5)

        pos_script = """
tell application "System Events"
    tell process "System Settings"
        activate
        delay 0.3
        if (count of windows) = 0 then return "no_window"
        set w to window 1
        set p to position of w
        set s to size of w
        return ((item 1 of p) as text) & "," & ((item 2 of p) as text) & "," & ((item 1 of s) as text) & "," & ((item 2 of s) as text)
    end tell
end tell
"""
        try:
            pos_str = run_osascript(pos_script, timeout=10, label="signout_get_window_pos").strip()
        except Exception as exc:
            return f"error:get_pos:{exc}"
        if pos_str == "no_window":
            return "no_window"
        try:
            wx, wy, ww, wh = [int(v.strip()) for v in pos_str.split(",")]
        except Exception:
            return f"error:bad_pos:{pos_str}"

        def _qclick(x: int, y: int) -> None:
            pt = Quartz.CGPointMake(float(x), float(y))
            for kind in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
                ev = Quartz.CGEventCreateMouseEvent(None, kind, pt, Quartz.kCGMouseButtonLeft)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                time.sleep(0.05)

        try:
            from ApplicationServices import (  # type: ignore[import]
                AXUIElementCreateApplication, AXUIElementCopyAttributeValue,
                AXValueGetValue, kAXChildrenAttribute, kAXRoleAttribute,
                kAXDescriptionAttribute, kAXValueAttribute, kAXTitleAttribute,
                kAXPositionAttribute, kAXSizeAttribute,
                kAXValueCGPointType, kAXValueCGSizeType,
            )
            _ax_ok = True
        except Exception:
            _ax_ok = False

        def _attr(el, a):
            try:
                err, val = AXUIElementCopyAttributeValue(el, a, None)
                return val if err == 0 else None
            except Exception:
                return None

        def _find_in_sys_settings(needles: "list[str]") -> "tuple[int, int] | None":
            """Center of the first System Settings element whose Description/Value/Title
            contains any needle (case-insensitive).  Rebuilds the AX tree each call so it
            reflects the currently-visible pane."""
            if not _ax_ok:
                return None
            try:
                ss_pid = int(subprocess.run(
                    ["osascript", "-e",
                     'tell application "System Events" to return unix id of process "System Settings"'],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip())
            except Exception:
                return None
            wanted = [n.lower() for n in needles]
            root = AXUIElementCreateApplication(ss_pid)
            stack = [root]
            seen = 0
            while stack and seen < 12000:
                el = stack.pop()
                seen += 1
                for attr in (kAXDescriptionAttribute, kAXValueAttribute, kAXTitleAttribute):
                    v = _attr(el, attr)
                    if isinstance(v, str) and any(n in v.lower() for n in wanted):
                        pv = _attr(el, kAXPositionAttribute)
                        sv = _attr(el, kAXSizeAttribute)
                        if pv and sv:
                            okp, pt = AXValueGetValue(pv, kAXValueCGPointType, None)
                            oks, sz = AXValueGetValue(sv, kAXValueCGSizeType, None)
                            if okp and oks:
                                try:
                                    return int(pt.x) + int(sz.width) // 2, int(pt.y) + int(sz.height) // 2
                                except Exception:
                                    pass
                        break
                ch = _attr(el, kAXChildrenAttribute)
                if ch:
                    stack.extend(ch)
            return None

        # Navigate the System Settings panes by AX label (resolution/scaling-independent),
        # falling back to the old fixed coordinates only when AX can't resolve an element.
        # 1) Apple Account sidebar row (labeled with the account name in the sidebar; the
        #    pane header reads "Apple Account" on Sequoia+, "Apple ID" on older macOS).
        acct_pos = _find_in_sys_settings(["Apple Account", "Apple ID"])
        _qclick(*(acct_pos or (wx + 84, wy + 113)))
        time.sleep(1.8)
        # 2) Media & Purchases (holds the Podcasts/media Sign Out button).
        media_pos = _find_in_sys_settings(["Media & Purchases", "Media and Purchases"])
        _qclick(*(media_pos or (int(wx + ww * 0.65), int(wy + wh * 0.82))))
        time.sleep(2.0)

        # 3) The Sign Out button itself.
        sign_out_pos = _find_in_sys_settings(["Sign Out"])
        if sign_out_pos:
            _qclick(*sign_out_pos)
            r2 = f"clicked_ax:{sign_out_pos[0]},{sign_out_pos[1]}"
        else:
            _qclick(int(wx + ww * 0.91), int(wy + wh * 0.69))
            r2 = f"clicked_coord:{int(wx + ww * 0.91)},{int(wy + wh * 0.69)}"
        time.sleep(1.0)

        step3 = """
tell application "System Events"
    set deadline to (current date) + 8
    repeat while (current date) < deadline
        try
            tell process "System Settings"
                repeat with w in windows
                    try
                        repeat with s in sheets of w
                            repeat with b in buttons of s
                                if name of b contains "Sign Out" then
                                    click b
                                    return "confirmed"
                                end if
                            end repeat
                        end repeat
                    end try
                    try
                        repeat with b in buttons of w
                            if name of b contains "Sign Out" then
                                click b
                                return "confirmed_window"
                            end if
                        end repeat
                    end try
                end repeat
            end tell
        end try
        delay 0.4
    end repeat
    return "no_confirmation"
end tell
"""
        try:
            r3 = run_osascript(step3, timeout=12, label="signout_confirm").strip()
        except Exception as exc:
            r3 = f"error:{exc}"

        # The AppleScript confirm above only sees buttons that are DIRECT children of the
        # sheet/window — on newer macOS the confirmation "Sign Out" button is nested in
        # groups, which is why it returned no_confirmation on every run.  Fall back to the
        # deep AX search + Quartz click, which walks the full tree.
        if "confirmed" not in r3:
            deadline = time.time() + 6
            while time.time() < deadline:
                pos = _find_in_sys_settings(["Sign Out"])
                if pos:
                    _qclick(*pos)
                    r3 = f"confirmed_ax:{pos[0]},{pos[1]}"
                    break
                time.sleep(0.4)
        time.sleep(2.0)
        try:
            run_osascript('tell application "System Settings" to quit', timeout=5, label="close_sys_settings")
        except Exception:
            pass

        # Verify the sign-out actually took effect instead of reporting a false success.
        # Give Podcasts a moment to reflect the change, then read the Account menu.
        self.activate()
        time.sleep(1.5)
        final_state = "unknown"
        for _ in range(4):
            final_state = self.check_sign_in_state()
            if final_state != "signed_in":
                break
            time.sleep(2.0)
        if final_state == "signed_in":
            self.logger.log(
                f"Sign-out UNVERIFIED — still signed in after confirm ({r3})", step="14a")
            return f"signout_unverified:{r3}"
        return f"signed_out:{r3}"

    def is_signed_out(self) -> bool:
        """True only when we can POSITIVELY confirm no Apple ID is signed in.

        Conservative on purpose: it must see BOTH no signed-in email AND the Account
        menu offering 'Sign In'. Any ambiguous/unknown reading returns False (treated
        as still signed in) so the caller keeps trying rather than removing shows.
        """
        self.activate()
        if "@" in self.get_signed_in_email():
            return False
        return self.check_sign_in_state() == "signed_out"

    def sign_out_confirmed(self, max_attempts: int = 4) -> bool:
        """Sign out and VERIFY it, retrying until confirmed signed out.

        Returns True only when the sign-out is confirmed (is_signed_out()). The
        underlying sign_out_of_podcasts() return string is NOT trusted — it has been
        seen to both under- and over-report — so success is decided solely by an
        independent post-check of the Account menu.
        """
        for attempt in range(1, max_attempts + 1):
            if self.is_signed_out():
                self.logger.log(
                    f"Sign-out confirmed (attempt {attempt}: already signed out)", step="14a")
                return True
            result = self.sign_out_of_podcasts()
            self.logger.log(
                f"Sign-out attempt {attempt}/{max_attempts}: {result}", step="14a")
            for _ in range(6):
                time.sleep(2)
                if self.is_signed_out():
                    self.logger.log(
                        f"Sign-out CONFIRMED after attempt {attempt}", step="14a")
                    return True
            self.logger.log(
                f"Sign-out attempt {attempt} did not confirm — retrying", step="14a")
        confirmed = self.is_signed_out()
        self.logger.log(
            f"Sign-out final state after {max_attempts} attempts: "
            f"{'confirmed' if confirmed else 'STILL SIGNED IN'}", step="14a")
        return confirmed

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
    def __init__(self, config: Config, log_dir: Path, state_path: Path,
                 input_path: Path | None = None):
        self.config = config
        self.input_path = input_path
        self.logger = RunLogger(log_dir)
        self.state = StateManager(state_path)
        self.net = NetworkState(self.logger)
        self.vpn = VPNController(self.logger, self.net, self.state)
        self.chrome = ChromeController(self.logger, self.state)
        self.podcasts = PodcastsController(self.logger, self.state)

    def _remove_account_from_input(self, email: str) -> str:
        """Remove the account with `email` from the input tasks.json, leaving the
        rest of the file (its formatting and every other key) untouched.

        Called once a cycle is fully complete so each account is dropped from the
        input file right after its own cycle finishes.  Works on the raw text so
        it only edits the single matching account line and re-normalizes the
        trailing commas on the remaining account lines — it does not re-serialize
        the whole file.  Validates the result parses as JSON before writing, so a
        bug can never corrupt the input file.  Non-fatal: returns a status string
        instead of raising.
        """
        path = self.input_path
        if path is None:
            return "no_input_path"
        try:
            text = path.read_text(encoding="utf-8-sig")
        except Exception as exc:
            return f"read_error:{exc}"
        lines = text.splitlines(keepends=True)
        # Locate the "accounts": [ ... ] block.
        start = next((i for i, ln in enumerate(lines)
                      if '"accounts"' in ln and '[' in ln), None)
        if start is None:
            return "accounts_key_not_found"
        end = next((j for j in range(start + 1, len(lines))
                    if lines[j].lstrip().startswith(']')), None)
        if end is None:
            return "accounts_close_not_found"
        target = next((k for k in range(start + 1, end)
                       if f'"{email}"' in lines[k]), None)
        if target is None:
            return "email_not_found"
        del lines[target]
        # Re-normalize trailing commas on the remaining account object lines: all
        # but the last get a trailing comma, the last gets none. Only touches the
        # comma after each `}` — the internal alignment of each line is preserved.
        new_end = end - 1
        remaining = list(range(start + 1, new_end))
        for pos, k in enumerate(remaining):
            ln = lines[k]
            nl = "\n" if ln.endswith("\n") else ""
            core = ln.rstrip("\n").rstrip()
            if core.endswith(","):
                core = core[:-1].rstrip()
            if pos != len(remaining) - 1:
                core += ","
            lines[k] = core + nl
        new_text = "".join(lines)
        try:
            json.loads(new_text)
        except json.JSONDecodeError as exc:
            return f"would_corrupt:{exc}"
        try:
            path.write_text(new_text, encoding="utf-8")
        except Exception as exc:
            return f"write_error:{exc}"
        return "removed"

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

                if self.config.accounts:
                    self.logger.log("Signing out before cleanup", step="14a")
                    self.podcasts.activate()
                    signed_out = self.podcasts.sign_out_confirmed(max_attempts=4)
                    self.logger.log(f"Sign-out confirmed before cleanup: {signed_out}",
                                    step="14a", cycle=cycle, signed_out=signed_out)
                    if not signed_out:
                        # HARD GATE: never remove shows while an Apple ID is still signed
                        # in. Abort before cleanup so downloads stay in place and the
                        # cycle is neither completed nor its account dropped.
                        self.state.record_failure(
                            step="14a", error="signout_unconfirmed_cleanup_blocked",
                            cycle=cycle,
                        )
                        raise AutomationError(
                            f"Cycle {cycle}: Apple ID sign-out could not be confirmed after "
                            f"retries — refusing to remove shows while still signed in"
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

                # This cycle's account is done — remove it from the input file so
                # each ID is dropped right after its own cycle completes. Only the
                # on-disk file changes; the in-memory config is untouched so the
                # remaining cycles keep their existing account indexing.
                if self.config.accounts:
                    used = self.config.accounts[(cycle - 1) % len(self.config.accounts)]
                    remove_result = self._remove_account_from_input(used.email)
                    self.logger.log(
                        f"Removed account from input after cycle {cycle}: "
                        f"{used.email} → {remove_result}",
                        step="15", cycle=cycle, email=used.email, result=remove_result,
                    )

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

    @staticmethod
    def _clean_show_title(chrome_title: str) -> str:
        """Strip Apple Podcasts suffixes from a Chrome tab title to get the show name.

        Returns "" if nothing usable remains (caller falls back to the AX heading).
        """
        cleaned = (chrome_title or "").strip()
        suffixes = (
            " - Apple Podcasts", " – Apple Podcasts", " — Apple Podcasts",
            " | Apple Podcasts",
            " - Podcast", " – Podcast", " — Podcast",
            " - Podcasts", " – Podcasts", " — Podcasts",
        )
        changed = True
        while changed:
            changed = False
            for sfx in suffixes:
                if cleaned.endswith(sfx):
                    cleaned = cleaned[: -len(sfx)].strip()
                    changed = True
                    break
        if cleaned.lower() in ("", "podcasts", "apple podcasts"):
            return ""
        return cleaned

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

        # Derive the expected show name from the Chrome tab title up-front so we can
        # confirm Podcasts actually navigated to THIS show before following/downloading.
        expected_show = self._clean_show_title(title)

        self.logger.log(f"Opening URL in Podcasts app: {podcast_url}", step="09")
        self.podcasts.open_url(podcast_url)
        self.podcasts.activate()
        self.podcasts.wait_for_window()
        # Navigation gate: on tab 2+ the window already exists from the previous show,
        # so wait_for_window returns instantly and Follow/download could run against the
        # PREVIOUS (already-followed) show — the root cause of "misses one podcast".
        # Block until the visible show matches the requested one.  Timeout allows for a
        # cold Podcasts launch (the between-cycle quit means the first tab of each new
        # cycle relaunches the app).
        load_state = self.podcasts.wait_for_show_loaded(expected_show, timeout_sec=40)
        if load_state == "timeout":
            # A cold launch can drop the first deep link — re-open once and wait again.
            self.logger.log(
                f"Show load timeout on tab {tab_task.tab}; re-opening {podcast_url} once",
                step="10", tab=tab_task.tab, status="reopen",
            )
            self.podcasts.open_url(podcast_url)
            self.podcasts.activate()
            self.podcasts.wait_for_window()
            load_state = self.podcasts.wait_for_show_loaded(expected_show, timeout_sec=40)
        self.logger.log(f"Show load state: {load_state}", step="10",
                        tab=tab_task.tab, expected_show=expected_show, status=load_state)
        if load_state == "timeout":
            # Still nothing rendered — log loudly and record, but do NOT abort: killing a
            # multi-cycle run over one slow load is worse than attempting the tab anyway
            # (follow/download degrade gracefully if the page really isn't there).
            self.logger.log(
                f"WARNING: Podcasts did not load show for tab {tab_task.tab} "
                f"({expected_show!r}) — continuing anyway",
                step="10", tab=tab_task.tab, status="show_load_timeout",
            )
            self.state.append_list(
                "load_warnings",
                {"cycle": cycle, "tab": tab_task.tab, "show": expected_show},
            )
        self.logger.log("Podcasts page loaded", step="10")

        # Sign in after the Podcasts window is up.
        # sign_in_to_podcasts returns immediately if already on the correct account,
        # so calling it on every tab is safe — it's a no-op after the first tab.
        if self.config.accounts:
            account = self.config.accounts[(cycle - 1) % len(self.config.accounts)]
            self.logger.log(f"Signing in with account {account.email}", step="10a")
            sign_in_result = self.podcasts.sign_in_to_podcasts(account.email, account.password)
            self.logger.log(
                f"Podcasts sign-in result: {sign_in_result}",
                step="10a", email=account.email, result=sign_in_result,
            )
            self.state.data.setdefault("sign_in_results", []).append(
                {"cycle": cycle, "tab": tab_task.tab, "email": account.email,
                 "result": sign_in_result}
            )
            self.state.save()

        # The "What's New in Apple Podcasts" modal can appear right after a fresh
        # sign-in and blocks the Follow button — dismiss it before Follow.
        self.podcasts.dismiss_continue_popup()

        # Follow the show, verifying the click actually took effect. A follow that
        # can't be confirmed is a real miss (downstream cleanup keys off it), so record
        # it loudly — but do NOT abort: episodes can still download, and killing a
        # multi-cycle run over one uncertain follow is worse than the miss.
        follow_result = self.podcasts.click_follow_button()
        self.logger.log(f"Follow button: {follow_result}", step="10b",
                        tab=tab_task.tab, status=follow_result)
        if follow_result == "follow_unconfirmed":
            self.logger.log(
                f"WARNING: follow could not be confirmed for tab {tab_task.tab} "
                f"({expected_show!r}) — continuing; show may be left unfollowed",
                step="10b", tab=tab_task.tab, status="follow_unconfirmed",
            )
            self.state.append_list(
                "follow_warnings",
                {"cycle": cycle, "tab": tab_task.tab, "show": expected_show},
            )

        # Show name for state-driven cleanup: prefer the cleaned Chrome tab title
        # (most reliable), else the Podcasts AX window title / heading.
        show_name = expected_show or self.podcasts.capture_show_name()
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

        # The "What's New" modal can also surface later, once the show finishes
        # rendering — dismiss it here too so it can't block See All / download /
        # cleanup (it persists until Continue is clicked). No-op if already gone.
        self.podcasts.dismiss_continue_popup()

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
        """Remove stale shows from Recently Updated before the first cycle begins.

        Called only when clean_start=True in tasks.json. Leaves the Downloaded
        section untouched — cleanup only ever acts on Recently Updated.
        """
        self.logger.log(
            "clean_start: checking Recently Updated section for stale items", step="00"
        )
        try:
            self.podcasts.activate()
            self.podcasts.wait_for_window()
            results = self.podcasts.cleanup_all_from_recently_updated()
            removed = sum(1 for r in results if "removed" in r.get("result", ""))
            self.logger.log(
                f"clean_start cleanup done: {removed} show(s) removed", step="00"
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

        # Remove every show from Recently Updated (Remove Download, then Unfollow
        # Show) until the section is confirmed empty. The Downloaded section is left
        # untouched — downloaded shows stay in place.
        results = self.podcasts.cleanup_all_from_recently_updated()

        for r in results:
            self.state.add_cleanup_result(cycle=cycle, **r)

        removed = sum(1 for r in results if "removed" in r.get("result", ""))
        self.state.mark_phase(cycle, "cleanup_completed")
        self.logger.log(
            f"Cleanup finished: {removed} show(s) removed ({len(results)} actions)",
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

    orch = Orchestrator(config, log_dir=args.output_dir, state_path=args.state,
                        input_path=args.input)
    return orch.run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
