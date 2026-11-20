"""Role-scoped sub-agents: planner parsing, persistence, and the tool subset.

The property that matters is enforcement, not suggestion: a role must actually
remove tools from the list the sub-agent is given, and no role may contain an
enqueue tool (a sub-agent must never spawn more work).
"""
from __future__ import annotations

import json

import pytest

from jarvis.agent.planner import Plan, PlanStep, decompose_goal
from jarvis.agent.roles import (
    DEFAULT_ROLE,
    ROLES,
    get_role,
    normalise_role,
    role_names,
    tools_for_role,
)
from jarvis.agent.task_runner import TaskStore


class _Tool:
    """Stand-in for AgentTool: tools_for_role only needs `.name`."""

    def __init__(self, name: str):
        self.name = name


ALL_TOOL_NAMES = [
    "web_search",
    "jarvis_rag_search",
    "notion_read",
    "google_read",
    "read_file",
    "list_dir",
    "search_files",
    "jarvis_projects_read",
    "get_current_time",
    "sandbox_execute",
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_screenshot",
    "browser_extract",
    "jarvis_task_enqueue",
    "jarvis_task_status",
    "jarvis_plan_and_enqueue",
]


def _engine_returning(steps):
    def call_engine(engine, msgs, tools):
        return {"content": json.dumps({"steps": steps})}

    return call_engine


# -- role definitions -------------------------------------------------------

def test_roles_are_known_and_default_exists():
    assert DEFAULT_ROLE in ROLES
    assert set(role_names()) >= {"researcher", "coder", "reviewer", "general"}


def test_unknown_role_degrades_to_general():
    assert normalise_role("wizard") == DEFAULT_ROLE
    assert normalise_role("") == DEFAULT_ROLE
    assert normalise_role(None) == DEFAULT_ROLE
    assert normalise_role("RESEARCHER") == "researcher"


@pytest.mark.parametrize("role", ["researcher", "coder", "reviewer", "general"])
def test_no_role_can_enqueue_more_work(role):
    """A sub-agent must never be able to spawn further tasks."""
    assert "jarvis_task_enqueue" not in ROLES[role].tools
    assert "jarvis_plan_and_enqueue" not in ROLES[role].tools


@pytest.mark.parametrize("role", ["researcher", "coder", "reviewer", "general"])
def test_every_role_tool_is_a_real_tool(role):
    """A typo in a role tuple would silently remove a capability."""
    unknown = set(ROLES[role].tools) - set(ALL_TOOL_NAMES)
    assert not unknown, f"{role} names tools that do not exist: {sorted(unknown)}"


def test_roles_actually_restrict():
    """The whole point: a role must be a strict subset, not the full list."""
    available = [_Tool(n) for n in ALL_TOOL_NAMES]
    researcher = {t.name for t in tools_for_role("researcher", available)}
    coder = {t.name for t in tools_for_role("coder", available)}
    assert "sandbox_execute" not in researcher
    assert "web_search" not in coder
    assert researcher != coder


def test_reviewer_cannot_fetch_new_material():
    """A reviewer that can search starts writing a new answer instead."""
    available = [_Tool(n) for n in ALL_TOOL_NAMES]
    reviewer = {t.name for t in tools_for_role("reviewer", available)}
    assert "web_search" not in reviewer
    assert "browser_navigate" not in reviewer
    assert "read_file" in reviewer


def test_unknown_role_gets_general_toolset():
    available = [_Tool(n) for n in ALL_TOOL_NAMES]
    got = {t.name for t in tools_for_role("wizard", available)}
    assert got == {t.name for t in tools_for_role("general", available)}


# -- planner ----------------------------------------------------------------

def test_planner_parses_role_per_step():
    plan = decompose_goal("g", 3, _engine_returning([
        {"index": 0, "goal": "find", "depends_on": [], "role": "researcher"},
        {"index": 1, "goal": "compute", "depends_on": [0], "role": "coder"},
        {"index": 2, "goal": "check", "depends_on": [1], "role": "reviewer"},
    ]))
    assert [s.role for s in plan.steps] == ["researcher", "coder", "reviewer"]


def test_planner_coerces_unknown_role_instead_of_failing():
    plan = decompose_goal("g", 2, _engine_returning([
        {"index": 0, "goal": "a", "depends_on": [], "role": "wizard"},
        {"index": 1, "goal": "b", "depends_on": [0]},
    ]))
    assert [s.role for s in plan.steps] == [DEFAULT_ROLE, DEFAULT_ROLE]


def test_planner_prompt_lists_available_roles():
    seen = {}

    def call_engine(engine, msgs, tools):
        seen["prompt"] = msgs[0]["content"]
        return {"content": json.dumps({"steps": [
            {"index": 0, "goal": "a", "depends_on": [], "role": "general"},
        ]})}

    decompose_goal("g", 1, call_engine)
    for name in role_names():
        assert name in seen["prompt"]


# -- persistence ------------------------------------------------------------

def test_role_persists_on_the_task_row(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    plan = Plan(root_goal="r", steps=[
        PlanStep(index=0, goal="find", role="researcher"),
        PlanStep(index=1, goal="compute", depends_on=[0], role="coder"),
    ])
    root, _ = store.create_plan(plan)
    roles = [t.role for t in store.get_plan_tasks(root.id) if not t.is_plan_root]
    assert roles == ["researcher", "coder"]


def test_role_survives_a_store_restart(tmp_path):
    path = tmp_path / "tasks.db"
    plan = Plan(root_goal="r", steps=[PlanStep(index=0, goal="a", role="reviewer")])
    root, _ = TaskStore(path).create_plan(plan)
    restored = TaskStore(path).get_plan_tasks(root.id)
    assert any(t.role == "reviewer" for t in restored)


def test_plan_step_defaults_to_general():
    assert PlanStep(index=0, goal="a").role == DEFAULT_ROLE
