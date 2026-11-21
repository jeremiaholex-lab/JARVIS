"""Host-loaded READ-only plugin tools (roadmap item #7, plugin system).

A plugin lives in this package, exposes ``tools() -> List[AgentTool]``, and
is discovered by :mod:`jarvis.agent.plugins` at host startup. The two
security rules that make this safe are enforced outside the plugin code:

1. A plugin tool may only register while the deterministic tier classifier
   calls its name READ (registry.py).
2. Only names allowlisted in ``jarvis.security.tiers`` are reachable, so a
   plugin can never add write or destructive capability.
"""
from __future__ import annotations
