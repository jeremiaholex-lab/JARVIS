"""Read-only laptop facts: battery, network, open windows, combined overview.

Every value here is observed locally or is explicit ``None`` — no guesses, no
fallback names, no saved credentials, no process command lines. Window titles
and process names go through Task 1's ``redact_sensitive()`` before leaving the
module, so the same filter covers facts, cards, and targeting alike.

psutil is optional: a host without it reports ``available: false`` rather than
failing. Window enumeration uses the existing Windows-only ScreenCapture API;
non-Windows hosts report an empty, explicitly unavailable app fact.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from jarvis.agent.tools import AgentTool
from jarvis.desktop.actions import redact_sensitive
from jarvis.security.tiers import Tier

try:
    import psutil
except ImportError:  # telemetry is optional; absence is a fact, not a guess
    psutil = None  # type: ignore[assignment]


def _unavailable(reason: str) -> Dict[str, Any]:
    return {"available": False, "reason": reason}


def battery_facts() -> Dict[str, Any]:
    """Battery percent, power state, and remaining time — or explicit None."""
    if psutil is None:
        return {**_unavailable("psutil unavailable"), "percent": None,
                "plugged_in": None, "seconds_left": None}
    try:
        battery = psutil.sensors_battery()
    except Exception as exc:
        return {**_unavailable(f"{exc.__class__.__name__}: {exc}"),
                "percent": None, "plugged_in": None, "seconds_left": None}
    if battery is None:
        return {**_unavailable("no battery detected"), "percent": None,
                "plugged_in": None, "seconds_left": None}
    plugged_in = (bool(battery.power_plugged)
                  if battery.power_plugged is not None else None)
    return {
        "available": True,
        "percent": round(float(battery.percent), 1),
        # psutil exposes plug state only. There is no reliable charging flag
        # without an OS-specific probe, so do not invent one.
        "plugged_in": plugged_in,
        "seconds_left": (int(battery.secsleft)
                         if getattr(battery, "secsleft", None) is not None
                         and battery.secsleft != psutil.POWER_TIME_UNKNOWN
                         else None),
    }


def network_facts() -> Dict[str, Any]:
    """Interface names, addresses, and up/down state. No keys or passwords."""
    if psutil is None:
        return {**_unavailable("psutil unavailable"), "interfaces": []}
    try:
        stats = psutil.net_if_stats()
        addresses = psutil.net_if_addrs()
    except Exception as exc:
        return {**_unavailable(f"{exc.__class__.__name__}: {exc}"), "interfaces": []}
    interfaces: List[Dict[str, Any]] = []
    for name, stat in stats.items():
        if not getattr(stat, "isup", False):
            continue
        # Interface names are host-assigned identifiers, not window titles;
        # redact anyway in case a user renamed one after a sensitive account.
        entry: Dict[str, Any] = {
            "name": redact_sensitive(str(name)),
            "is_up": True,
            "addresses": [],
        }
        for addr in addresses.get(name, ()):
            if getattr(addr, "family", None) is None:
                continue
            family = getattr(addr.family, "name", str(addr.family))
            if str(family).upper() not in {"AF_INET", "AF_INET6"}:
                continue  # never surface hardware addresses as extra identity
            value = getattr(addr, "address", None)
            if value:
                entry["addresses"].append(str(value))
        interfaces.append(entry)
    return {"available": True, "interfaces": interfaces}


def normalize_windows(windows: Any) -> List[Dict[str, Any]]:
    """Project raw windows to safe facts: title, process, pid, rectangle only.

    Both title and process pass through the shared redactor. Sensitive windows
    are kept as ``[REDACTED]`` entries so counts remain honest; no hwnd, class
    name, command line, or raw title is copied into facts. A caller that has a
    real rectangle may pass one; otherwise it stays ``None`` — the Windows
    enumerator does not report geometry, and a guessed rect would be a lie.
    """
    facts: List[Dict[str, Any]] = []
    for window in windows or ():
        if isinstance(window, dict):
            get = window.get
        else:
            get = lambda key, default=None: getattr(window, key, default)
        raw_title = get("title", "") or ""
        raw_process = get("process", "") or ""
        rect = get("rect", None) or get("rectangle", None)
        facts.append({
            "title": redact_sensitive(str(raw_title)),
            "process": redact_sensitive(str(raw_process)),
            "pid": get("pid", None),
            "rect": rect if isinstance(rect, (list, tuple)) else None,
        })
    return facts


def open_app_facts() -> Dict[str, Any]:
    """Visible titled windows, redacted; unavailable on non-Windows hosts."""
    if os.name != "nt":
        return {**_unavailable("window enumeration requires Windows"),
                "apps": []}
    try:
        from jarvis.vision.screen import ScreenCapture

        windows = ScreenCapture().list_windows()
    except Exception as exc:
        return {**_unavailable(f"{exc.__class__.__name__}: {exc}"), "apps": []}
    return {"available": True, "apps": normalize_windows(windows)}


def laptop_overview() -> Dict[str, Any]:
    """Compact combined answer for 'what is happening on my laptop?'."""
    apps = open_app_facts()
    return {
        "available": True,
        "battery": battery_facts(),
        "network": network_facts(),
        "apps": apps["apps"],
        "apps_available": apps["available"],
    }


def _as_json(fn):
    def _run() -> str:
        try:
            return json.dumps(fn())
        except Exception as exc:
            return json.dumps(_unavailable(f"{exc.__class__.__name__}: {exc}"))
    return _run


def tools() -> List[AgentTool]:
    """Four READ-only telemetry tools, allowlisted in jarvis.security.tiers."""
    return [
        AgentTool(name="system_battery_status",
                  description="Read-only battery percent, plug state, and time remaining.",
                  parameters={"type": "object", "properties": {}},
                  fn=_as_json(battery_facts), tier=Tier.READ),
        AgentTool(name="system_network_status",
                  description="Read-only active network interface names, addresses, and states.",
                  parameters={"type": "object", "properties": {}},
                  fn=_as_json(network_facts), tier=Tier.READ),
        AgentTool(name="system_open_apps",
                  description="Read-only visible window titles, process names, PIDs, and rectangles.",
                  parameters={"type": "object", "properties": {}},
                  fn=_as_json(open_app_facts), tier=Tier.READ),
        AgentTool(name="system_overview",
                  description="Compact read-only battery, network, and open-app snapshot of this laptop.",
                  parameters={"type": "object", "properties": {}},
                  fn=_as_json(laptop_overview), tier=Tier.READ),
    ]


__all__ = [
    "battery_facts", "network_facts", "normalize_windows",
    "open_app_facts", "laptop_overview", "tools",
]
