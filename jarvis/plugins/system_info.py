"""Sample plugin: a read-only host CPU/memory/disk snapshot.

This is the reference plugin — the pattern any new plugin module in
``jarvis/plugins/`` should follow. It exposes one AgentTool; its name
(``system_info``) is allowlisted in ``jarvis.security.tiers`` as READ, and
the registry will reject it the moment that classification changes.

The tool returns compact JSON so the model can reason about the host. All
reads are local; nothing here mutates the host or calls out.
"""
from __future__ import annotations

import json
import os
from typing import List

from jarvis.agent.tools import AgentTool
from jarvis.security.tiers import Tier


def _system_info() -> str:
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        drive = os.environ.get("SystemDrive", "C:") + "\\" if os.name == "nt" else "/"
        disk = psutil.disk_usage(drive)
        payload = {
            "cpu_percent": cpu,
            "mem_total_mb": mem.total // (1024 * 1024),
            "mem_available_mb": mem.available // (1024 * 1024),
            "disk_total_gb": disk.total // (1024 ** 3),
            "disk_free_gb": disk.free // (1024 ** 3),
        }
    except Exception as exc:  # psutil absent or a probe failed: degrade, don't crash
        payload = {"error": f"{exc.__class__.__name__}: {exc}"}
    return json.dumps(payload)


def tools() -> List[AgentTool]:
    """The plugin's tool surface. One READ-only tool, allowlisted by name."""
    return [
        AgentTool(
            name="system_info",
            description=(
                "Read-only snapshot of this host: CPU %, total/available memory, "
                "and free/total disk for the system drive. Use it when the user "
                "asks about the machine's current resources."
            ),
            parameters={"type": "object", "properties": {}},
            fn=_system_info,
            tier=Tier.READ,
        )
    ]
