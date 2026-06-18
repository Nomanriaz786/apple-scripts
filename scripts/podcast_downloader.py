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
from dataclasses import dataclass
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
class VPNConfig:
    enabled: bool
    app: str = "Proton VPN"
    location: str = "United States"
    location_code: str = "US"
    servers: tuple[str, ...] = ()
    require_provider_in_org: bool = True
    verify_timeout: int = DEFAULT_VERIFY_TIMEOUT_SEC


@dataclass(frozen=True)
class Config:
    repeat: int
    vpn: VPNConfig
    cleanup: bool
    tabs: list[TabTask]
    check_downloads: bool = False
    clean_start: bool = False


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

    return VPNConfig(
        enabled=enabled,
        app=app,
        location=location,
        location_code=location_code,
        servers=servers,
        require_provider_in_org=require,
        verify_timeout=int(raw_vpn.get("verify_timeout", DEFAULT_VERIFY_TIMEOUT_SEC)),
    )


def load_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8-sig"))

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

    return Config(
        repeat=repeat,
        vpn=vpn,
        cleanup=cleanup,
        tabs=tabs,
        check_downloads=check_downloads,
        clean_start=clean_start,
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
            if cached:
                servers = list(cached)
                self.logger.log(
                    f"Using cached server list for {vpn_cfg.location} ({len(servers)} servers)",
                    step="06", location=vpn_cfg.location, source="cache",
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
                servers = self._discover_servers(vpn_cfg.location, vpn_cfg.location_code)
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
            time.sleep(2.5)
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

        # Build a try-order: start at the cycle-determined slot, wrap through all.
        # If one slot's VPN never establishes (server down/busy), we disconnect
        # and fall through to the next slot automatically.
        start_idx = (cycle - 1) % len(servers)
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
                force_retype=(attempt_i > 0),
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

    def _discover_servers(self, location: str, location_code: str) -> list[str]:
        """Return slot-based server names for position-driven rotation.

        ProtonVPN uses lazy rendering — server row text is never populated in the
        accessibility tree until the row is hovered.  Rather than trying to read
        names, we return positional slot tokens (e.g. US-SLOT-1 … US-SLOT-5).
        _connect_via_slot translates a slot token into a hover+click on the Nth
        visible server row in the expanded country list.
        """
        self.logger.log(
            f"Slot-based discovery for {location}: returning 5 positional slots",
            step="06", location=location,
        )
        return [f"{location_code.upper()}-SLOT-{i + 1}" for i in range(5)]

    def _click_server_by_name(
        self, app_name: str, server: str, location: str = "", force_retype: bool = False
    ) -> str:
        """Route to slot-based connect or (future) named-server connect."""
        if "-SLOT-" in server:
            try:
                slot_num = int(server.split("-SLOT-")[1])
            except (ValueError, IndexError):
                slot_num = 1
            return self._connect_via_slot(
                app_name, location or server.split("-SLOT-")[0], slot_num,
                force_retype=force_retype,
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
        self, app_name: str, location: str, slot_num: int, force_retype: bool = False
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
        Connect button appears at right_edge - 38 px on hover.
        """
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
        title_bar_x = w_x + w_w // 2
        title_bar_y = w_y + 12

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
            time.sleep(3.0)  # wait for ProtonVPN to apply the filter

            self.logger.log(f"Search filter pasted: '{location}'", step="06")
        finally:
            # Restore original clipboard.
            subprocess.run(["pbcopy"], input=old_clip, check=False)

        # ── Phase 3: scroll to top, read row-2 position ──────────────────────────────
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
                if not (exists scroll area 1 of window 1) then return "ERROR:no_scroll_area"
                if not (exists table 1 of scroll area 1 of window 1) then return "ERROR:no_table"
                set sc to scroll area 1 of window 1
                set tbl to table 1 of sc
                try
                    set value of scroll bar 1 of sc to 0
                    delay 0.3
                end try
                if not (exists row 1 of tbl) then return "ERROR:no_rows_after_search"
                set cRow to missing value
                if exists row 2 of tbl then
                    set cRow to row 2 of tbl
                else
                    set cRow to row 1 of tbl
                end if
                set r2Pos to position of cRow
                set r2X to (item 1 of r2Pos) as integer
                set r2Top to (item 2 of r2Pos) as integer
                set wPos to position of window 1
                set wSz to size of window 1
                set wX to (item 1 of wPos) as integer
                set wY to (item 2 of wPos) as integer
                set wW to (item 1 of wSz) as integer
                return "R2:" & r2X & "," & r2Top & "|W:" & wX & "," & wY & "," & wW
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
        w_x2 = w_y2 = w_w2 = 0
        for chunk in p3.split("|"):
            if chunk.startswith("R2:"):
                nums = chunk[3:].split(",")
                if len(nums) == 2:
                    r2_x, r2_top = int(nums[0]), int(nums[1])
            elif chunk.startswith("W:"):
                nums = chunk[2:].split(",")
                if len(nums) == 3:
                    w_x2, w_y2, w_w2 = int(nums[0]), int(nums[1]), int(nums[2])

        # Use phase3 window coords if available (most current), fall back to phase1.
        if w_w2 > 0:
            w_x, w_y, w_w = w_x2, w_y2, w_w2

        if r2_top == 0 or w_w == 0:
            self.logger.log(f"Bad phase3 data: {p3!r}", step="06")
            return "bad_anchor_data"

        US_HEADER_H = 48   # United States country header row height (empirically measured)
        SERVER_ROW_H = 48  # Individual server row height

        expand_x = w_x + w_w // 2
        expand_y = r2_top + US_HEADER_H // 2
        connect_x = w_x + w_w - 38
        server_y = r2_top + US_HEADER_H + (slot_num - 1) * SERVER_ROW_H + SERVER_ROW_H // 2
        title_bar_x = w_x + w_w // 2
        title_bar_y = w_y + 12

        self.logger.log(
            f"Slot {slot_num}: r2=({r2_x},{r2_top}) w=({w_x},{w_y},{w_w}) "
            f"expand=({expand_x},{expand_y}) server_y={server_y} connect_x={connect_x}",
            step="06", slot=slot_num,
        )

        # ── Phase 4: expansion check → hover → click Connect ─────────────────────────

        def _sample_max_brightness(sx, sy, sw=40, sh=20):
            rect = Quartz.CGRectMake(sx, sy, sw, sh)
            img = Quartz.CGWindowListCreateImage(
                rect, Quartz.kCGWindowListOptionOnScreenOnly,
                Quartz.kCGNullWindowID, Quartz.kCGWindowImageDefault,
            )
            if img is None:
                return 0
            dp = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(img))
            if not dp or len(dp) < 4:
                return 0
            return max(dp[i] + dp[i + 1] + dp[i + 2] for i in range(0, len(dp) - 3, 4))

        # Raise ProtonVPN and click title bar to ensure window focus for hover effects.
        subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to set frontmost of process "ProtonVPN" to true'],
            timeout=4, check=False,
        )
        time.sleep(0.2)
        _mouse(Quartz.kCGEventLeftMouseDown, title_bar_x, title_bar_y)
        _mouse(Quartz.kCGEventLeftMouseUp,   title_bar_x, title_bar_y)
        time.sleep(0.2)

        brightness = _sample_max_brightness(w_x + 10, server_y - 10)
        # 308 = fully expanded server row, 117 = collapsed (dark background).
        # 245 = highlighted/recently-connected row — treat as NOT expanded.
        is_expanded = brightness > 270
        self.logger.log(
            f"Pixel expansion check: brightness={brightness} expanded={is_expanded}",
            step="06",
        )

        if not is_expanded:
            _mouse(Quartz.kCGEventLeftMouseDown, expand_x, expand_y)
            _mouse(Quartz.kCGEventLeftMouseUp,   expand_x, expand_y)
            time.sleep(1.5)
        else:
            self.logger.log("Server list already expanded — skipping expand click", step="06")

        hover_x = w_x + w_w // 2
        _mouse(Quartz.kCGEventMouseMoved, hover_x, server_y)
        time.sleep(0.5)
        for x in range(hover_x + 20, connect_x, 20):
            _mouse(Quartz.kCGEventMouseMoved, x, server_y)
        _mouse(Quartz.kCGEventMouseMoved, connect_x, server_y)
        time.sleep(0.6)

        _mouse(Quartz.kCGEventLeftMouseDown, connect_x, server_y)
        time.sleep(0.1)
        _mouse(Quartz.kCGEventLeftMouseUp,   connect_x, server_y)
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
        """Return 'clicked', 'list_already_expanded', or 'see_all_not_found'.

        Walks the whole accessibility tree (bounded depth) looking for ANY
        element whose name, description, or value matches a "See All" variant,
        then tries multiple click methods (direct click and AXPress) because
        Apple Podcasts implements the control as a styled link/static-text,
        not always as a true AXButton.
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
            if t is "Show All" then return true
            if t is "See all" then return true
            if t contains "See All" then return true
            if t contains "Show All" then return true
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
        delay 8.0
        tell application "System Events"
            tell process "Podcasts"
                set frontmost to true
                delay 0.5
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
                        delay 0.9
                        return "clicked"
                    end if
                    if (current date) > deadline then return "see_all_not_found"
                    delay 1.5
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
                delay 0.4
            end tell
        end tell
        """
        run_osascript(script, timeout=5, label="scroll to top")

    def download_episode_row(self, video_no: int) -> str:
        """Click the download (↓) button for the Nth episode.

        The download button is hover-only — absent from the AX tree until the
        mouse physically hovers the row.  Strategy:
          1. BFS to find the Nth episode button and read its pixel rect.
          2. Navigate into it to find the 'more' (⋯) button center.
          3. Quartz: move mouse to row center → pause for hover state → click at
             (more_x - 35, more_y), which is where the download icon sits.
        """
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
                        set dd to ""
                        try
                            set dd to description of elem as string
                        end try
                        -- Episode rows start with a day name (TODAY, YESTERDAY, MON-SUN)
                        -- and are tall buttons (height > 50px); playback controls are small.
                        set looksLikeEpisode to false
                        if dd contains ", " and length of dd > 20 then
                            set upDD to dd
                            if upDD starts with "TODAY" or upDD starts with "YESTERDAY" or upDD starts with "MON" or upDD starts with "TUE" or upDD starts with "WED" or upDD starts with "THU" or upDD starts with "FRI" or upDD starts with "SAT" or upDD starts with "SUN" or upDD starts with "JAN" or upDD starts with "FEB" or upDD starts with "MAR" or upDD starts with "APR" or upDD starts with "MAY" or upDD starts with "JUN" or upDD starts with "JUL" or upDD starts with "AUG" or upDD starts with "SEP" or upDD starts with "OCT" or upDD starts with "NOV" or upDD starts with "DEC" then
                                set looksLikeEpisode to true
                            end if
                            if not looksLikeEpisode then
                                -- Fall back to height check for other date formats
                                try
                                    set sz to size of elem
                                    if (item 2 of sz) > 50 then set looksLikeEpisode to true
                                end try
                            end if
                        end if
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

                return "ROW:" & eX & "," & eY & "," & eW & "," & eH & "|MORE:" & moreX & "," & moreY
            end tell
        end tell
        """

        out = run_osascript(script, timeout=90, label=f"find episode {video_no} position")

        # If episode wasn't visible, scroll down and retry once — the episode list
        # lazy-loads rows as the viewport scrolls; the first BFS sees only visible rows.
        if out.startswith("ERROR:episode_not_found"):
            import re as _re
            seen_m = _re.search(r"seen=(\d+)", out)
            seen_n = int(seen_m.group(1)) if seen_m else 0
            if seen_n > 0 and seen_n < video_no:
                self.logger.log(
                    f"Download episode {video_no}: seen={seen_n} rows — scrolling down",
                    step="13",
                )
                try:
                    import Quartz as _Q  # type: ignore[import]
                    row_h_est = 120
                    scroll_px = (video_no - seen_n + 2) * row_h_est
                    ev = _Q.CGEventCreateScrollWheelEvent(
                        None, _Q.kCGScrollEventUnitPixel, 1, -scroll_px
                    )
                    content_cx = 1060  # center of content area (stable across window sizes)
                    content_cy = 450
                    _Q.CGEventSetLocation(ev, _Q.CGPointMake(content_cx, content_cy))
                    _Q.CGEventPost(_Q.kCGHIDEventTap, ev)
                    time.sleep(0.9)
                    out = run_osascript(script, timeout=90, label=f"find episode {video_no} position (retry)")
                except ImportError:
                    pass

        if out.startswith("ERROR:"):
            self.logger.log(f"Download episode {video_no}: {out}", step="13")
            return "download_not_found"

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

        if row_w == 0:
            self.logger.log(
                f"Download episode {video_no}: bad position in '{out}'", step="13"
            )
            self._dump_ax_tree(f"download_row_{video_no}_not_found")
            return "download_not_found"

        # Phase 2: activate Podcasts, then Quartz hover → pixel-click download icon
        try:
            run_osascript(
                'tell application "Podcasts" to activate',
                timeout=5, label="activate Podcasts before download click",
            )
            time.sleep(0.3)
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
        time.sleep(0.6)

        # Move cursor to the download button position before clicking
        # (keeps the hover state active since dl_x,dl_y is still within the row)
        _mouse(Quartz.kCGEventMouseMoved, dl_x, dl_y)
        time.sleep(0.15)

        # Click the download button
        _mouse(Quartz.kCGEventLeftMouseDown, dl_x, dl_y)
        time.sleep(0.1)
        _mouse(Quartz.kCGEventLeftMouseUp, dl_x, dl_y)
        time.sleep(0.5)

        return "download_clicked"

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
                        set dd to ""
                        try
                            set dd to description of elem as string
                        end try
                        set looksLikeEpisode to false
                        if dd contains ", " and length of dd > 20 then
                            set upDD to dd
                            if upDD starts with "TODAY" or upDD starts with "YESTERDAY" or upDD starts with "MON" or upDD starts with "TUE" or upDD starts with "WED" or upDD starts with "THU" or upDD starts with "FRI" or upDD starts with "SAT" or upDD starts with "SUN" or upDD starts with "JAN" or upDD starts with "FEB" or upDD starts with "MAR" or upDD starts with "APR" or upDD starts with "MAY" or upDD starts with "JUN" or upDD starts with "JUL" or upDD starts with "AUG" or upDD starts with "SEP" or upDD starts with "OCT" or upDD starts with "NOV" or upDD starts with "DEC" then
                                set looksLikeEpisode to true
                            end if
                            if not looksLikeEpisode then
                                try
                                    set sz to size of elem
                                    if (item 2 of sz) > 50 then set looksLikeEpisode to true
                                end try
                            end if
                        end if
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
        time.sleep(0.6)
        _mouse(Quartz.kCGEventMouseMoved, more_x, more_y)
        time.sleep(0.3)
        _mouse(Quartz.kCGEventLeftMouseDown, more_x, more_y)
        time.sleep(0.1)
        _mouse(Quartz.kCGEventLeftMouseUp, more_x, more_y)
        time.sleep(1.5)

        ss = self._take_screenshot("cleanup_context_menu")
        self.logger.log(f"⋯ clicked, screenshot: {ss}", step="14")

        # Down×1+Enter → "Remove Download" (first item in episode ⋯ menu)
        _key(0x7D, True); _key(0x7D, False)
        time.sleep(0.3)
        _key(0x24, True); _key(0x24, False)
        time.sleep(1.5)

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

        The Downloaded nav item is a UI element (NOT a button) in the AX tree.
        Use BFS to find its pixel center then Quartz-click it.
        """
        script = """
        tell application "System Events"
            set frontmost of process "Podcasts" to true
        end tell
        delay 0.3
        tell application "System Events"
            tell process "Podcasts"
                if not (exists window 1) then return "ERROR:no_window"
                set q to {window 1}
                set deadline to (current date) + 20
                repeat 600 times
                    if (count of q) = 0 then exit repeat
                    if (current date) > deadline then return "ERROR:deadline"
                    set elem to item 1 of q
                    if (count of q) > 1 then
                        set q to items 2 thru -1 of q
                    else
                        set q to {}
                    end if
                    set dd to ""
                    try
                        set dd to description of elem as string
                    end try
                    -- Exact match to avoid matching episode-row descriptions that
                    -- contain "Downloaded" as a suffix.
                    if dd is "Downloaded" then
                        set cl to class of elem as string
                        if cl is "UI element" then
                            set sz to size of elem
                            -- Sidebar nav item is ~180×28px; skip small episode-row labels
                            if (item 1 of sz) > 100 then
                                set pos to position of elem
                                set cx to ((item 1 of pos) + (item 1 of sz) / 2) as integer
                                set cy to ((item 2 of pos) + (item 2 of sz) / 2) as integer
                                return "CENTER:" & cx & "," & cy
                            end if
                        end if
                    end if
                    try
                        repeat with ch in UI elements of elem
                            set end of q to ch
                        end repeat
                    end try
                end repeat
                return "ERROR:not_found"
            end tell
        end tell
        """
        out = run_osascript(script, timeout=30, label="find Downloaded nav item")
        if out.startswith("ERROR:"):
            self.logger.log(f"navigate_to_downloaded_tab: {out}", step="14")
            return "not_found"

        try:
            cx, cy = (int(v) for v in out.replace("CENTER:", "").split(","))
        except ValueError:
            return "not_found"

        try:
            import Quartz  # type: ignore[import]
        except ImportError:
            return "quartz_unavailable"

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
        time.sleep(1.5)

        self.logger.log(f"Clicked Downloaded sidebar at ({cx},{cy})", step="14")
        return "navigated"

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

    def _find_downloaded_card_frame(self) -> tuple[int, int, int, int] | None:
        """BFS for the first show card in the Downloaded tab content area.

        Returns (x, y, w, h) from the actual AXFrame of the card element.
        No hardcoded window offsets — the position comes from the AX tree so it
        works regardless of window size or position.
        Accepts any element that is in the content area (right of sidebar),
        roughly square (0.5 < W/H < 2), and between 80–450 px per side.
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
                set deadline to (current date) + 14
                repeat 800 times
                    if (count of q) = 0 then exit repeat
                    if (current date) > deadline then exit repeat
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
                    -- Card criteria: in content area, right size, roughly square aspect ratio.
                    -- No class filter — Mac Catalyst exposes cards under various roles.
                    if eX > contentLeft and eW >= 80 and eH >= 80 and eW <= 450 and eH <= 450 then
                        -- Aspect ratio roughly square: W/H between 0.5 and 2
                        if eW * 2 > eH and eW < eH * 2 then
                            return "CARD:" & eX & "," & eY & "," & eW & "," & eH & "|WIN:" & wX & "," & wY & "," & wW & "," & wH
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
        out = run_osascript(script, timeout=22, label="find downloaded card frame")
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

    def wait_for_downloads_stable(self, timeout: int = 180) -> str:
        """Wait for all downloads to finish before cleanup starts.

        Strategy (in order):
          1. Check for AXProgressIndicator elements in the window (most reliable).
          2. Check for 'Downloading' text via check_downloads_state().
          3. If neither detector fires, wait a fixed 45s fallback and log it.

        Returns one of: 'completed' | 'in_progress_polled' | 'stable_unknown' | 'timeout'.
        State fields written: download_state, download_wait_seconds.
        """
        nav = self.navigate_to_downloaded_tab()
        if nav not in ("navigated",):
            self.logger.log(f"wait_for_downloads: nav failed ({nav}) — fallback 120s", step="14")
            time.sleep(120)
            self.state.data["download_state"] = "stable_unknown"
            self.state.data["download_wait_seconds"] = 120
            self.state.save()
            return "stable_unknown"

        time.sleep(5)  # let download register in Downloaded tab view

        progress_check_script = """
        tell application "System Events"
            tell process "Podcasts"
                if not (exists window 1) then return "0"
                set cnt to 0
                try
                    set cnt to count (every UI element of entire contents of window 1 whose role is "AXProgressIndicator")
                end try
                return cnt as text
            end tell
        end tell
        """

        def _has_progress_indicators() -> bool:
            try:
                out = run_osascript(progress_check_script, timeout=10, label="progress indicator check")
                return int(out.strip()) > 0
            except (AutomationError, ValueError):
                return False

        def _has_text_in_progress() -> bool:
            try:
                s = self.check_downloads_state()
                return s.get("status") == "downloads_in_progress"
            except Exception:
                return False

        t_start = time.time()
        initial_progress = _has_progress_indicators()
        initial_text = _has_text_in_progress() if not initial_progress else True

        if not initial_progress and not initial_text:
            # Neither detector found active downloads — unknown state.
            # Use 120s fallback: podcast episodes commonly take 1-3 min to download.
            self.logger.log(
                "Download state: no active indicators detected — waiting 120s (stable_unknown)",
                step="14", download_state="stable_unknown",
            )
            time.sleep(120)
            waited = int(time.time() - t_start)
            self.state.data["download_state"] = "stable_unknown"
            self.state.data["download_wait_seconds"] = waited
            self.state.save()
            return "stable_unknown"

        # Active downloads detected — poll until they finish
        self.logger.log(
            f"Downloads active (progress_indicators={initial_progress}, text={initial_text})"
            f" — polling up to {timeout}s",
            step="14", download_state="in_progress",
        )
        deadline = time.time() + timeout
        poll_interval = 5
        while time.time() < deadline:
            time.sleep(poll_interval)
            still_going = _has_progress_indicators() or _has_text_in_progress()
            elapsed = int(time.time() - t_start)
            self.logger.log(
                f"Download poll: active={still_going} elapsed={elapsed}s", step="14",
            )
            if not still_going:
                self.state.data["download_state"] = "completed"
                self.state.data["download_wait_seconds"] = elapsed
                self.state.save()
                self.logger.log(f"Downloads completed after {elapsed}s", step="14",
                                download_state="completed", download_wait_seconds=elapsed)
                return "completed"

        elapsed = int(time.time() - t_start)
        self.logger.log(
            f"Download wait timed out after {elapsed}s — proceeding with cleanup anyway",
            step="14", download_state="timeout", download_wait_seconds=elapsed,
        )
        self.state.data["download_state"] = "timeout"
        self.state.data["download_wait_seconds"] = elapsed
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
        time.sleep(0.8)
        _mouse(Quartz.kCGEventMouseMoved, three_dots_x, three_dots_y)
        time.sleep(0.4)
        _mouse(Quartz.kCGEventLeftMouseDown, three_dots_x, three_dots_y)
        time.sleep(0.1)
        _mouse(Quartz.kCGEventLeftMouseUp, three_dots_x, three_dots_y)
        time.sleep(1.5)

        return "three_dots_clicked"

    def _click_confirmation_remove(self) -> str:
        """Click the destructive Remove button in the confirmation sheet.

        After the context menu's Remove item is activated, Podcasts shows a native
        macOS sheet (accessible via AX) with a Remove From Library button.
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
        # The sheet appears on screen quickly but can take up to ~20s to become
        # AX-accessible as 'sheet 1 of window 1'.  Retry for up to 30 seconds.
        out = "no_sheet"
        for _attempt in range(20):
            out = run_osascript(script, timeout=10, label="click Remove in confirmation sheet")
            if out != "no_sheet":
                break
            time.sleep(1.5)
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
        # 15s: ProtonVPN kill-switch leaves no default route; Podcasts needs extra
        # time to load episode metadata through the VPN tunnel on cycle 2+.
        time.sleep(15)

        see_all_status = self.click_see_all()
        if see_all_status in ("error", "see_all_not_found"):
            return f"see_all_failed:{see_all_status}"
        self.scroll_to_top()
        time.sleep(1.0)

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
            time.sleep(0.7)

            self.logger.log(
                f"Cleanup episode {video_no}: clicked ⋯ at ({more_x},{more_y})", step="14"
            )

            # Try AX menu first; keyboard fallback otherwise
            ax_ok = self._click_remove_menu_item_ax()
            if not ax_ok:
                # Down×4 = 'Remove Download' in the episode row context menu
                import subprocess as _sp
                _sp.run(
                    ["osascript", "-e",
                     'tell application "System Events" to key code 125\n'
                     'tell application "System Events" to key code 125\n'
                     'tell application "System Events" to key code 125\n'
                     'tell application "System Events" to key code 125\n'
                     'tell application "System Events" to key code 36'],
                    timeout=5, check=False,
                )
                self.logger.log(f"Cleanup episode {video_no}: keyboard Down×4+Enter used", step="14")
                self.state.data["cleanup_fallback_keyboard_used"] = True

            removed.append(video_no)
            time.sleep(1.2)

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
                        set dd to ""
                        try
                            set dd to description of elem as string
                        end try
                        set looksLikeEpisode to false
                        if dd contains ", " and length of dd > 20 then
                            set upDD to dd
                            if upDD starts with "TODAY" or upDD starts with "YESTERDAY" or upDD starts with "MON" or upDD starts with "TUE" or upDD starts with "WED" or upDD starts with "THU" or upDD starts with "FRI" or upDD starts with "SAT" or upDD starts with "SUN" or upDD starts with "JAN" or upDD starts with "FEB" or upDD starts with "MAR" or upDD starts with "APR" or upDD starts with "MAY" or upDD starts with "JUN" or upDD starts with "JUL" or upDD starts with "AUG" or upDD starts with "SEP" or upDD starts with "OCT" or upDD starts with "NOV" or upDD starts with "DEC" then
                                set looksLikeEpisode to true
                            end if
                            if not looksLikeEpisode then
                                try
                                    set sz to size of elem
                                    if (item 2 of sz) > 50 then set looksLikeEpisode to true
                                end try
                            end if
                        end if
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
        time.sleep(0.8)
        _mouse(Quartz.kCGEventMouseMoved, three_dots_x, three_dots_y)
        time.sleep(0.4)
        _mouse(Quartz.kCGEventLeftMouseDown, three_dots_x, three_dots_y)
        time.sleep(0.1)
        _mouse(Quartz.kCGEventLeftMouseUp, three_dots_x, three_dots_y)
        time.sleep(1.5)

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
                time.sleep(0.3)
            _key(0x24, True); _key(0x24, False)
            time.sleep(1.5)

        # ── Confirmation sheet ────────────────────────────────────────────────
        remove = self._click_confirmation_remove()
        time.sleep(1.0)
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
        """Try to click a 'Remove' menu item via Accessibility. Returns True on success.

        Looks in:
          1. Any floating window whose role contains 'menu' or 'AXMenu'.
          2. menus of window 1.
        """
        script = """
        tell application "System Events"
            tell process "Podcasts"
                -- Check all windows for a floating context menu
                repeat with w in windows
                    set wRole to ""
                    try
                        set wRole to role of w as string
                    end try
                    if wRole contains "AXMenu" then
                        repeat with mi in menu items of w
                            set mName to ""
                            try
                                set mName to name of mi as string
                            end try
                            if mName contains "Remove" or mName contains "Delete" then
                                click mi
                                return "ax_clicked"
                            end if
                        end repeat
                    end if
                end repeat
                -- Also check menus attached to window 1
                repeat with m in menus of window 1
                    repeat with mi in menu items of m
                        set mName to ""
                        try
                            set mName to name of mi as string
                        end try
                        if mName contains "Remove" or mName contains "Delete" then
                            click mi
                            return "ax_clicked"
                        end if
                    end repeat
                end repeat
                return "menu_not_found"
            end tell
        end tell
        """
        try:
            result = run_osascript(script, timeout=5, label="AX Remove menu item")
            return result == "ax_clicked"
        except AutomationError:
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
        time.sleep(1.5)

        remove_by_ax = self._click_remove_menu_item_ax()
        if not remove_by_ax:
            self.logger.log("Generic cleanup: keyboard fallback", step="14",
                            status="fallback_keyboard_remove_used")
            self.state.data["cleanup_fallback_keyboard_used"] = True
            self.state.save()
            for _ in range(3):
                _key(0x7D, True); _key(0x7D, False)
                time.sleep(0.3)
            _key(0x24, True); _key(0x24, False)
            time.sleep(1.5)

        remove = self._click_confirmation_remove()
        time.sleep(1.0)

        if "clicked" in remove:
            return "removed"
        elif remove == "no_sheet":
            return "no_confirm_dialog"
        return f"remove_failed:{remove}"

    def quit_app(self) -> None:
        if HAS_PYXA:
            try:
                PyXA.Application("Podcasts").quit()
                return
            except Exception:
                pass
        run_osascript('tell application "Podcasts" to quit', label="quit Podcasts")


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
                f"cleanup={self.config.cleanup} clean_start={self.config.clean_start} "
                f"tabs={len(self.config.tabs)}",
                step="02",
            )
            self.logger.log(f"Loaded runtime state: {self.state.path}", step="03",
                            completed_cycles=self.state.data["completed_cycles"])
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

                    for tab_task in self.config.tabs:
                        self._process_tab(tab_task, cycle)

                    self.state.mark_phase(cycle, "all_tabs_completed")

                if self.config.cleanup:
                    self._cleanup_phase(cycle)

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
        time.sleep(10)
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
        if see_all_result == "see_all_not_found":
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

        for video_no in tab_task.videos:
            self.state.update(current_video=video_no)
            self.logger.log(f"Target video {video_no} searching", step="13", video=video_no)
            status = self.podcasts.download_episode_row(video_no)
            self.logger.log(f"Target video {video_no} {status}", step="13",
                            video=video_no, status=status)
            self.state.mark_phase(cycle, f"video_{tab_task.tab}_{video_no}_download_clicked")
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

        Called only when clean_start=True in tasks.json.  Checks the Downloaded
        tab for leftover items from a previous failed run and removes them.
        """
        self.logger.log("clean_start: checking Downloaded tab for stale items", step="00")
        try:
            self.podcasts.activate()
            self.podcasts.wait_for_window()
            nav = self.podcasts.navigate_to_downloaded_tab()
            if nav != "navigated":
                self.logger.log(f"clean_start: Downloaded nav failed ({nav})", step="00")
                return
            time.sleep(2)
            frame = self.podcasts._find_downloaded_card_frame()
            if frame is None:
                self.logger.log("clean_start: no stale items found", step="00")
                return
            self.logger.log("clean_start: stale items found — cleaning up", step="00")
            results = self.podcasts.cleanup_all_downloaded()
            self.logger.log(f"clean_start cleanup done: {len(results)} actions", step="00")
        except Exception as exc:
            self.logger.log(f"clean_start cleanup error (non-fatal): {exc}", step="00")

    def _cleanup_phase(self, cycle: int) -> None:
        self.state.mark_phase(cycle, "cleanup_started")
        self.podcasts.activate()
        self.podcasts.wait_for_window()

        self.logger.log("Cleanup phase start", step="14", cycle=cycle)

        # Wait for all downloads to complete before removing anything.
        dl_status = self.podcasts.wait_for_downloads_stable(timeout=180)
        self.logger.log(
            f"Download wait: {dl_status} "
            f"(state={self.state.data.get('download_state')} "
            f"waited={self.state.data.get('download_wait_seconds')}s)",
            step="14", cycle=cycle,
        )
        self.state.mark_phase(cycle, "downloads_stable")

        # Collect full show info from this cycle's processed_shows for targeted cleanup.
        # Primary path: navigate to each show's episode list and remove downloaded episodes
        # via the episode row ⋯ menu (AX-accessible, unlike Downloaded tab cards).
        cycle_shows: list[dict[str, Any]] = self.state.data.get("processed_shows", {}).get(str(cycle), [])
        valid_shows = [
            s for s in cycle_shows
            if s.get("show_name") and s["show_name"] != "unknown_show" and s.get("url")
        ]

        if valid_shows:
            show_names = [s["show_name"] for s in valid_shows]
            self.logger.log(f"Cleanup: targeting shows by name: {show_names}", step="14", cycle=cycle)
            results = self.podcasts.cleanup_by_show_info(valid_shows)
        else:
            # No show info captured — fall back to generic card-based cleanup
            self.logger.log(
                "Cleanup: no show info in state — using generic card cleanup", step="14", cycle=cycle,
            )
            results = self.podcasts.cleanup_all_downloaded()

        for r in results:
            self.state.add_cleanup_result(cycle=cycle, **r)

        self.state.mark_phase(cycle, "cleanup_completed")
        self.logger.log(f"Cleanup finished: {len(results)} actions", step="14",
                        action_count=len(results))


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

    config = load_config(args.input)
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
