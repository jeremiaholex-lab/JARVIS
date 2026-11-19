"""JARVIS agent layer — the pieces the orb conversation path runs through.

The design (docs/jarvis-agent-design.md §6.4, §6.5, §6.8) describes the
assistant as more than a chat completion: routing between local and cloud
brains, durable conversation memory, and agent tools (projects, deadlines,
document search) available during an ordinary turn. Before this package the
orb called the LLM directly, so none of that applied to it.

Modules:
  routing.py — decides local vs. cloud per turn, with a stated reason.
  memory.py  — persistent episodic store (SQLite), survives reconnects.
  tools.py   — read-only agent tools, tier-checked before exposure.
  turn.py    — one full turn: route -> tools -> answer -> fallback.
"""
