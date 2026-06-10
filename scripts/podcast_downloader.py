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
DEFAULT_SEE_ALL_BUDGET_SEC = 12
DEFAULT_ACCESSIBILITY_DEPTH = 10
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

    return Config(repeat=repeat, vpn=vpn, cleanup=cleanup, tabs=tabs)


# -----------------------------------------------------------------------------
# State Manager
# -----------------------------------------------------------------------------
class StateManager:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict[str, Any] = self._load_or_init()

    def _load_or_init(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
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
            "cleanup_results": [],
            "see_all_state": {},
            "last_failed_step": None,
            "last_error": None,
            "resume_available": True,
            "started_at": now,
            "updated_at": now,
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
        raise AutomationError(f"osascript failed ({label}): {proc.stderr.strip()}")
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

    def public_ip_info(self) -> dict[str, Any] | None:
        try:
            req = urllib.request.Request(
                "https://ipinfo.io/json",
                headers={"User-Agent": "podcast-downloader/2.0"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))
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

        # Pick this cycle's server via deterministic rotation.
        target_server = servers[(cycle - 1) % len(servers)]
        self.logger.log(
            f"Cycle {cycle} target {vpn_cfg.app} server: {target_server} "
            f"(server {((cycle - 1) % len(servers)) + 1} of {len(servers)})",
            step="06", cycle=cycle, target_server=target_server,
        )

        # Ensure app is open.
        if not self._open_provider_app(vpn_cfg.app):
            raise AutomationError(f"{vpn_cfg.app} app not found.")

        # Baseline IP.
        baseline = self.net.public_ip_info()
        baseline_ip = (baseline or {}).get("ip")
        self.logger.log(
            f"Baseline IP: {baseline_ip} country={(baseline or {}).get('country')}",
            step="06", baseline_ip=baseline_ip,
        )

        # Disconnect any current tunnel so we connect to the requested server fresh.
        disc = self._click_disconnect(vpn_cfg.app)
        self.logger.log(f"Pre-connect disconnect: {disc}", step="06", status=disc)
        if disc == "disconnect_clicked":
            time.sleep(2.5)

        ui_status = self._click_server_by_name(vpn_cfg.app, target_server)
        self.logger.log(
            f"{vpn_cfg.app} server '{target_server}': {ui_status}",
            step="06", status=ui_status, server=target_server,
        )
        if ui_status not in ("server_clicked", "row_clicked", "connect_button_clicked"):
            raise AutomationError(
                f"Could not click server '{target_server}' in {vpn_cfg.app}: {ui_status}"
            )

        result = self._poll_verify(
            target_cc=vpn_cfg.location_code,
            provider_token=provider_token,
            require_provider_in_org=vpn_cfg.require_provider_in_org,
            verify_timeout=vpn_cfg.verify_timeout,
        )
        if result == "connected_verified":
            self._record_server(target_server)
        return result

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
        """Open VPN, search location, expand, and return visible server names."""
        loc_esc = location.replace('"', '\\"')
        prefix = location_code.upper() + "-"
        process_list = self._process_name_candidates("Proton VPN")
        script = _BOUNDED_HELPERS + """
        on findFirstTextField(rootElem, maxDepth)
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
                        if (class of elem) is text field then return elem
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
        end findFirstTextField

        on findElemByText(rootElem, txt, maxDepth)
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
                    set nn to ""
                    try
                        set nn to name of elem
                    end try
                    set vv to ""
                    try
                        set vv to (value of elem) as text
                    end try
                    if (nn contains txt) or (vv contains txt) then return elem
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
        end findElemByText

        on collectServerNames(rootElem, prefix, maxDepth)
            tell application "System Events"
                set found to {}
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
                    set nn to ""
                    try
                        set nn to name of elem
                    end try
                    set vv to ""
                    try
                        set vv to (value of elem) as text
                    end try
                    set candidate to ""
                    if (nn starts with prefix) and (nn contains "#") then
                        set candidate to nn
                    else if (vv starts with prefix) and (vv contains "#") then
                        set candidate to vv
                    end if
                    if candidate is not "" then
                        -- strip trailing city/whitespace after the server token (e.g. "US-AZ#81  Phoenix")
                        set trimmed to candidate
                        try
                            set AppleScript's text item delimiters to " "
                            set parts to text items of trimmed
                            set trimmed to item 1 of parts
                            set AppleScript's text item delimiters to ""
                        end try
                        if (count of found) is 0 or not (found contains trimmed) then
                            set end of found to trimmed
                        end if
                    end if
                    if d < maxDepth then
                        try
                            repeat with child in UI elements of elem
                                set end of stack to {child, d + 1}
                            end repeat
                        end try
                    end if
                end repeat
                return found
            end tell
        end collectServerNames

        tell application "System Events"
            set procName to ""
            repeat with candidate in {__PROCS__}
                if exists process (candidate as text) then
                    set procName to candidate as text
                    exit repeat
                end if
            end repeat
            if procName is "" then return "ERROR:vpn_process_not_found"

            tell process procName
                set frontmost to true
                delay 0.6
                if not (exists window 1) then return "ERROR:no_window"

                -- Click Countries tab if present.
                try
                    set ctab to my findButtonByName(window 1, "Countries", 6)
                    if ctab is not missing value then
                        click ctab
                        delay 0.4
                    end if
                end try

                -- Type the location name in the search field.
                set sf to my findFirstTextField(window 1, 8)
                if sf is missing value then return "ERROR:no_search_field"
                try
                    set focused of sf to true
                    delay 0.2
                end try
                try
                    set value of sf to ""
                end try
                delay 0.2
                keystroke "__LOCATION__"
                delay 1.2

                -- Find the matching country row and expand it.
                set locRow to my findElemByText(window 1, "__LOCATION__", 10)
                if locRow is missing value then return "ERROR:location_not_found"

                set expanded to false
                try
                    repeat with b in buttons of locRow
                        set dd to ""
                        try
                            set dd to description of b
                        end try
                        set bn to ""
                        try
                            set bn to name of b
                        end try
                        if (dd contains "more") or (dd contains "Show") or (dd contains "Expand") or (dd contains "disclos") or (bn contains "disclos") then
                            click b
                            delay 1.0
                            set expanded to true
                            exit repeat
                        end if
                    end repeat
                end try

                if not expanded then
                    -- Some lists expand on row click.
                    try
                        click locRow
                        delay 1.0
                    end try
                end if

                -- Walk the whole window for entries whose name matches "<prefix>...#"
                set servers to my collectServerNames(window 1, "__PREFIX__", 14)
                if (count of servers) is 0 then return "ERROR:no_servers_visible"

                set result to ""
                repeat with s in servers
                    if result is "" then
                        set result to (s as text)
                    else
                        set result to result & "|" & (s as text)
                    end if
                end repeat
                return result
            end tell
        end tell
        """.replace("__LOCATION__", loc_esc).replace("__PREFIX__", prefix).replace("__PROCS__", process_list)
        try:
            out = run_osascript(script, timeout=30, label=f"discover {location} servers")
        except AutomationError as exc:
            self.logger.log(
                f"Server discovery failed: {exc}",
                step="06", status="discovery_failed", error=str(exc),
            )
            return []
        if out.startswith("ERROR:"):
            self.logger.log(
                f"Server discovery: {out}",
                step="06", status=out,
            )
            return []
        return [s.strip() for s in out.split("|") if s.strip()]

    def _click_server_by_name(self, app_name: str, server: str) -> str:
        """Search VPN app for the server name, click its row. Returns a status string."""
        server_esc = server.replace('"', '\\"')
        process_list = self._process_name_candidates(app_name)
        script = _BOUNDED_HELPERS + """
        on findFirstTextField(rootElem, maxDepth)
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
                        if (class of elem) is text field then return elem
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
        end findFirstTextField

        on findElemByText(rootElem, targetText, maxDepth)
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
                    set elemText to ""
                    try
                        set elemText to (value of static texts of elem) as text
                    end try
                    set elemName to ""
                    try
                        set elemName to name of elem
                    end try
                    if (elemText contains targetText) or (elemName contains targetText) then return elem
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
        end findElemByText

        tell application "System Events"
            set procName to ""
            repeat with candidate in {__PROCS__}
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

                -- Make sure the Countries tab is showing (Profiles tab might be active).
                try
                    set ctab to my findButtonByName(window 1, "Countries", 5)
                    if ctab is not missing value then
                        click ctab
                        delay 0.3
                    end if
                end try

                -- Focus the search field and type the server name.
                set sf to my findFirstTextField(window 1, 8)
                if sf is missing value then return "search_field_not_found"
                try
                    set focused of sf to true
                    delay 0.2
                end try
                try
                    set value of sf to ""
                end try
                delay 0.2
                keystroke "__SERVER__"
                delay 1.0

                -- Find the element whose text/name matches the server.
                set targetElem to my findElemByText(window 1, "__SERVER__", 10)
                if targetElem is missing value then return "server_not_found"

                -- Prefer clicking a Connect button inside the row, fall back to the row itself.
                try
                    set cbtn to my findButtonByName(targetElem, "Connect", 4)
                    if cbtn is not missing value then
                        click cbtn
                        return "connect_button_clicked"
                    end if
                end try
                try
                    click targetElem
                    return "row_clicked"
                end try
                try
                    repeat with b in buttons of targetElem
                        click b
                        return "server_clicked"
                    end repeat
                end try
                return "click_failed"
            end tell
        end tell
        """.replace("__SERVER__", server_esc).replace("__PROCS__", process_list)
        return run_osascript(script, timeout=25, label=f"click server {server}")

    def _record_ip(self, ip: str | None) -> None:
        if not ip:
            return
        self.state.data["last_public_ip"] = ip
        if ip not in self.state.data["used_public_ips"]:
            self.state.data["used_public_ips"].append(ip)
        self.state.save()

    def _click_disconnect(self, app_name: str) -> str:
        process_list = self._process_name_candidates(app_name)
        script = _BOUNDED_HELPERS + """
        tell application "System Events"
            set procName to ""
            repeat with candidate in {__PROCS__}
                if exists process (candidate as text) then
                    set procName to candidate as text
                    exit repeat
                end if
            end repeat
            if procName is "" then return "vpn_process_not_found"

            tell process procName
                set frontmost to true
                delay 0.4
                set disc to my findButtonByName(window 1, "Disconnect", 5)
                if disc is not missing value then
                    click disc
                    return "disconnect_clicked"
                end if
                return "no_disconnect_button"
            end tell
        end tell
        """.replace("__PROCS__", process_list)
        return run_osascript(script, timeout=10, label=f"{app_name} disconnect")

    def _poll_verify(
        self,
        target_cc: str,
        provider_token: str,
        require_provider_in_org: bool,
        verify_timeout: int,
    ) -> str:
        deadline = time.monotonic() + verify_timeout
        attempts = 0
        last_info: dict[str, Any] | None = None
        used_ips = self.state.data["used_public_ips"]

        while time.monotonic() < deadline:
            attempts += 1
            time.sleep(1)
            info = self.net.public_ip_info()
            last_info = info
            if not info:
                continue
            if not self._is_connected_to(
                info, target_cc, require_provider_in_org, provider_token,
            ):
                continue
            ip = info.get("ip")
            if ip in used_ips:
                continue
            self._record_ip(ip)
            self.logger.log(
                f"VPN connected verified: ip={ip} country={info.get('country')} org={info.get('org')} "
                f"(after {attempts}s)",
                step="06", status="connected_verified",
                ip=ip, country=info.get("country"), attempts=attempts,
            )
            return "connected_verified"

        seen = last_info or {}
        raise AutomationError(
            f"VPN verification failed after {verify_timeout}s. "
            f"ip={seen.get('ip')} country={seen.get('country')} "
            f"wanted={target_cc} provider_token={provider_token}"
        )


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

        on findSeeAllElement(rootElem, maxDepth)
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
        delay 1.5
        tell application "System Events"
            tell process "Podcasts"
                set frontmost to true
                delay 0.6
                if not (exists window 1) then return "no_window"

                set elem to my findSeeAllElement(window 1, __DEPTH__)
                if elem is missing value then return "see_all_not_found"

                set clickResult to my attemptClick(elem)
                if clickResult is "click_failed" then return "see_all_click_failed"
                delay 0.9
                return "clicked"
            end tell
        end tell
        """.replace("__DEPTH__", str(DEFAULT_ACCESSIBILITY_DEPTH))
        return run_osascript(script, timeout=time_budget_sec + 15, label="click See All")

    def scroll_to_top(self) -> None:
        script = """
        tell application "System Events"
            tell process "Podcasts"
                set frontmost to true
                key code 115
                delay 0.4
            end tell
        end tell
        """
        run_osascript(script, timeout=5, label="scroll to top")

    def download_episode_row(self, video_no: int) -> str:
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
                        repeat with g in groups of elem
                            set t to my textOfElement(g)
                            if length of t > 20 then set end of candidates to g
                        end repeat
                    end try
                    try
                        repeat with r in rows of elem
                            set t to my textOfElement(r)
                            if length of t > 20 then set end of candidates to r
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

        on clickDownloadIn(rowElem, maxDepth)
            tell application "System Events"
                set stack to {{rowElem, 0}}
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
                            set nn to ""
                            try
                                set nn to name of b
                            end try
                            set dd to ""
                            try
                                set dd to description of b
                            end try
                            set lbl to nn & " " & dd
                            if lbl contains "Downloading" then return "already_downloading"
                            if lbl contains "Downloaded" then return "already_downloaded"
                            if lbl contains "Download" then
                                click b
                                return "download_clicked"
                            end if
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
            return "not_found"
        end clickDownloadIn

        property targetIndex : __TARGET__

        tell application "Podcasts" to activate
        tell application "System Events"
            tell process "Podcasts"
                set frontmost to true
                set seenText to {}
                set currentNumber to 0

                repeat with scrollPass from 1 to 60
                    set rowsNow to my collectRows(window 1, 5)
                    repeat with rowElem in rowsNow
                        set rowText to my textOfElement(rowElem)
                        if rowText is not "" and seenText does not contain rowText then
                            set end of seenText to rowText
                            set currentNumber to currentNumber + 1
                            if currentNumber is targetIndex then
                                set s to my clickDownloadIn(rowElem, 4)
                                if s is "not_found" then return "download_control_not_found"
                                return s
                            end if
                        end if
                    end repeat
                    key code 125
                    key code 125
                    key code 125
                    delay 0.25
                end repeat
            end tell
        end tell
        return "target_row_not_found"
        """.replace("__TARGET__", str(video_no))
        return run_osascript(script, timeout=90, label=f"download row {video_no}")

    def open_downloaded_sidebar(self) -> str:
        script = """
        tell application "Podcasts" to activate
        tell application "System Events"
            tell process "Podcasts"
                set frontmost to true
                try
                    click (first UI element of window 1 whose name contains "Downloaded")
                    delay 1
                    return "downloaded_opened"
                end try
            end tell
        end tell
        return "downloaded_not_found"
        """
        return run_osascript(script, timeout=10, label="open Downloaded sidebar")

    def cleanup_all_downloaded(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        terminal_states = (
            "more_button_not_found",
            "no_more_items",
            "no_confirm_dialog",
            "remove_menu_not_found",
            "no_window",
        )
        for i in range(50):
            res = self._cleanup_one_item()
            results.append({"iteration": i + 1, "result": res})
            if res in terminal_states or res.startswith("error:"):
                break
            time.sleep(0.6)
        return results

    def _cleanup_one_item(self) -> str:
        script = _BOUNDED_HELPERS + """
        tell application "Podcasts" to activate
        tell application "System Events"
            tell process "Podcasts"
                set frontmost to true
                delay 0.3
                if not (exists window 1) then return "no_window"

                set moreBtn to my findButtonByDesc(window 1, "More", 6)
                if moreBtn is missing value then return "more_button_not_found"

                click moreBtn
                delay 0.6

                try
                    click menu item "Remove…" of menu 1 of moreBtn
                    delay 0.5
                on error
                    try
                        click menu item "Remove..." of menu 1 of moreBtn
                        delay 0.5
                    on error
                        return "remove_menu_not_found"
                    end try
                end try

                -- Confirm dialog may attach to any window as a sheet. Search them all.
                set confirmBtn to missing value
                try
                    repeat with w in windows
                        set confirmBtn to my findButtonByName(w, "Remove from Library", 6)
                        if confirmBtn is not missing value then exit repeat
                    end repeat
                end try
                if confirmBtn is not missing value then
                    click confirmBtn
                    delay 1.0
                    return "removed"
                end if

                return "no_confirm_dialog"
            end tell
        end tell
        """
        try:
            return run_osascript(script, timeout=20, label="cleanup one item")
        except AutomationError as exc:
            return f"error: {exc}"

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
                f"cleanup={self.config.cleanup} tabs={len(self.config.tabs)}",
                step="02",
            )
            self.logger.log(f"Loaded runtime state: {self.state.path}", step="03",
                            completed_cycles=self.state.data["completed_cycles"])
            self._validate_environment()

            for cycle in range(1, self.config.repeat + 1):
                if cycle in self.state.data["completed_cycles"]:
                    self.logger.log(f"Cycle {cycle} already completed — skipping", step="05",
                                    cycle=cycle, status="skipped_resume")
                    continue

                # Clear any stale failure state from a prior run before starting fresh.
                self.state.data["last_failed_step"] = None
                self.state.data["last_error"] = None
                self.state.update(current_cycle=cycle)
                self.logger.log(f"Starting cycle {cycle}", step="05", cycle=cycle)

                if self.config.vpn.enabled:
                    self.vpn.connect_with_config(cycle=cycle, vpn_cfg=self.config.vpn)
                else:
                    self.logger.log("VPN disabled", step="06", status="vpn_disabled")

                self.chrome.activate()
                tabs_cache = self.chrome.enumerate_tabs()
                self.logger.log(f"Detected Chrome tabs ({len(tabs_cache)} found)", step="04",
                                tab_count=len(tabs_cache))

                for tab_task in self.config.tabs:
                    self._process_tab(tab_task, cycle)

                if self.config.cleanup:
                    self._cleanup_phase(cycle)

                self.podcasts.quit_app()

                completed = list(self.state.data["completed_cycles"])
                completed.append(cycle)
                self.state.update(completed_cycles=completed,
                                  current_tab=None, current_video=None)
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
        env = {"platform": platform.system(), "has_pyxa": HAS_PYXA}
        self.logger.log(f"Environment: {env}", step="03", **env)
        if platform.system() != "Darwin":
            raise AutomationError("This script must run on macOS")
        if not HAS_PYXA:
            raise AutomationError(
                "PyXA not installed. Run: python3 -m pip install -r requirements.txt"
            )

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
        self.logger.log("Podcasts page loaded", step="10")

        see_all_result = self.podcasts.click_see_all()
        self.logger.log(f"See All {see_all_result}", step="11", status=see_all_result)
        self.state.data.setdefault("see_all_state", {})[str(tab_task.tab)] = see_all_result
        self.state.save()
        if see_all_result == "see_all_not_found":
            self.state.record_failure(step="11", error="see_all_not_found",
                                      current_tab=tab_task.tab)
            raise AutomationError(f"See All button not found on tab {tab_task.tab}")

        self.podcasts.scroll_to_top()
        self.logger.log("Episode list reset to top", step="12")

        for video_no in tab_task.videos:
            self.state.update(current_video=video_no)
            self.logger.log(f"Target video {video_no} searching", step="13", video=video_no)
            status = self.podcasts.download_episode_row(video_no)
            self.logger.log(f"Target video {video_no} {status}", step="14",
                            video=video_no, status=status)
            self.state.add_task_result(
                cycle=cycle, tab=tab_task.tab, video=video_no,
                status=status, url=url, title=title,
            )

    def _cleanup_phase(self, cycle: int) -> None:
        self.logger.log("Opening Downloaded sidebar", step="14")
        opened = self.podcasts.open_downloaded_sidebar()
        self.logger.log(f"Downloaded sidebar: {opened}", step="14", status=opened)
        if opened != "downloaded_opened":
            self.logger.log("Skipping cleanup: sidebar not accessible", step="14")
            return
        results = self.podcasts.cleanup_all_downloaded()
        for r in results:
            self.state.add_cleanup_result(cycle=cycle, **r)
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
    args = parser.parse_args(argv)

    config = load_config(args.input)
    orch = Orchestrator(config, log_dir=args.output_dir, state_path=args.state)
    return orch.run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
