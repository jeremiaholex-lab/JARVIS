"""Role-scoped sub-agents (design: 2026-11-19-roles-and-preference-learning).

A role is a named subset of tools plus a short prompt addendum. The planner
names a role per sub-task; the runtime filters that sub-task's tool list by
it. The restriction is enforced here, not asked for in a prompt, so it holds
even when the model ignores its instructions.

Two invariants every role must keep:

  * it is a subset of READ-tier tools, so a background sub-task still cannot
    write, send, or mutate anything;
  * it contains no enqueue tool, so a sub-agent can never spawn more work.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence


@dataclass(slots=True)
class AgentRole:
    name: str
    description: str
    tools: tuple[str, ...]
    system_addendum: str = field(default="")


# Read anything, report findings. No computation, no browser state changes.
_RESEARCHER_TOOLS = (
    "web_search",
    "jarvis_rag_search",
    "notion_read",
    "google_read",
    "read_file",
    "list_dir",
    "search_files",
    "jarvis_projects_read",
    "get_current_time",
)

# Compute and inspect. Reads files so it can work from real input, but has no
# write tool of any kind — sandbox_execute only runs inside the sandbox.
_CODER_TOOLS = (
    "sandbox_execute",
    "read_file",
    "list_dir",
    "search_files",
    "get_current_time",
)

# Checks someone else's work. Deliberately narrower than the researcher: a
# reviewer that can fetch new material starts writing a new answer instead of
# reviewing the one it was given.
_REVIEWER_TOOLS = (
    "read_file",
    "list_dir",
    "search_files",
    "jarvis_rag_search",
    "get_current_time",
)

_GENERAL_TOOLS = (
    "jarvis_projects_read",
    "jarvis_rag_search",
    "google_read",
    "web_search",
    "read_file",
    "list_dir",
    "search_files",
    "notion_read",
    "get_current_time",
    "sandbox_execute",
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_screenshot",
    "browser_extract",
)

ROLES: dict[str, AgentRole] = {
    "researcher": AgentRole(
        name="researcher",
        description="Finds and cites facts from the user's data and the web.",
        tools=_RESEARCHER_TOOLS,
        system_addendum=(
            "You are acting as a researcher. Gather only what the task asks for, "
            "cite where each fact came from, and stop. If the task needs code to "
            "be run or a page to be driven, say which role should do that instead."
        ),
    ),
    "coder": AgentRole(
        name="coder",
        description="Runs sandboxed Python to compute, parse, or verify.",
        tools=_CODER_TOOLS,
        system_addendum=(
            "You are acting as a coder. Use sandbox_execute for computation and "
            "read_file for input. You cannot write files: report the code and its "
            "output instead of trying to save anything."
        ),
    ),
    "reviewer": AgentRole(
        name="reviewer",
        description="Checks work already done for errors and gaps.",
        tools=_REVIEWER_TOOLS,
        system_addendum=(
            "You are acting as a reviewer. Judge the work described in the task "
            "against its stated goal. Report concrete problems in priority order, "
            "and say plainly when there are none."
        ),
    ),
    "general": AgentRole(
        name="general",
        description="Default role when no specialist fits.",
        tools=_GENERAL_TOOLS,
        system_addendum="",
    ),
}

DEFAULT_ROLE = "general"


def normalise_role(name: str | None) -> str:
    """Coerce a planner-supplied role to a known one.

    An unknown name degrades to `general` rather than failing the plan: a
    hallucinated role should cost a worse tool set, not the whole task.
    """
    key = (name or "").strip().casefold()
    return key if key in ROLES else DEFAULT_ROLE


def get_role(name: str | None) -> AgentRole:
    return ROLES[normalise_role(name)]


def role_names() -> list[str]:
    return list(ROLES)


def tools_for_role(role: str | None, available: Iterable) -> list:
    """The subset of `available` tools this role may use.

    `available` is a sequence of AgentTool objects (or anything with `.name`).
    """
    allowed = set(get_role(role).tools)
    return [tool for tool in available if getattr(tool, "name", "") in allowed]


__all__ = [
    "AgentRole",
    "ROLES",
    "DEFAULT_ROLE",
    "normalise_role",
    "get_role",
    "role_names",
    "tools_for_role",
]
