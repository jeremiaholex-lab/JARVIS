"""Deadline-management skill wrapper (prototype).

Wraps the Phase 3 deadline CLI so agent turns can drive it. NOTE: this is an
early prototype — the runtime's skill system exposes SkillManager/
SkillRegistry, not the ``@register_skill`` decorator this was drafted
against, so registration is wired here as a no-op until the JARVIS-side
registration path is built.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from openjarvis.skills import register_skill
except ImportError:  # runtime skill API has no @register_skill; prototype no-op
    def register_skill(fn):  # type: ignore[misc]
        return fn

from project_store import ProjectStore
from deadline_cli import DeadlineCLI

class DeadlineSkill:
    """Skill wrapper for deadline management commands.
    Registers the CLI commands with OpenJarvis.
    """

    def __init__(self, project_store: ProjectStore):
        self.project_store = project_store
        self.cli = DeadlineCLI(project_store)

    @register_skill
    async def handle_deadline_command(self, command: str, args: List[str]) -> str:
        """Handle deadline management commands from the user.
        Supports: deadline add/update/list
        """
        try:
            return self.cli.run(args)
        except Exception as e:
            return f"Error: {str(e)}"

    @register_skill
    async def get_proactive_deadlines(self) -> List[Dict]:
        """Expose proactive deadline detection for other agents.
        """
        return self.project_store.get_proactive_deadlines()
