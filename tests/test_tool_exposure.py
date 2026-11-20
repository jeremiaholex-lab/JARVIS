"""Guard: every tool build_tools() intends to expose must survive the tier gate.

The failure this prevents: a tool is written, registered in build_tools(), and
then silently dropped because ``classify()`` returns WRITE for a name missing
from the READ set in ``jarvis/security/tiers.py``. ``_check_tier`` logs a
warning and returns None, so the tool vanishes from the conversation with no
error. That is how ``jarvis_plan_and_enqueue`` and the six sandbox/browser
tools were unreachable while the suite stayed green.
"""
from __future__ import annotations

import pytest

from jarvis.agent.tools import build_tools
from jarvis.security.tiers import PlannedAction, Tier, classify

# Tools that build_tools() is expected to expose to the conversation. Kept
# explicit so a rename or a dropped registration fails here rather than
# disappearing quietly.
EXPECTED_READ_TOOLS = {
    "jarvis_projects_read",
    "jarvis_rag_search",
    "google_read",
    "web_search",
    "read_file",
    "list_dir",
    "search_files",
    "notion_read",
    "jarvis_task_enqueue",
    "jarvis_task_status",
    "jarvis_plan_and_enqueue",
    "get_current_time",
    "look_at_screen",
    "sandbox_execute",
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_screenshot",
    "browser_extract",
}

# Advertised proposers: they only queue a pending change, never mutate.
EXPECTED_PROPOSER_TOOLS = {
    "jarvis_projects_write",
    "jarvis_file_edit",
}

# Local writes: WRITE-tier, but execute() runs them directly because they
# touch nothing outside the machine and need no confirmation.
EXPECTED_LOCAL_WRITE_TOOLS = {
    "jarvis_remember",
}


@pytest.fixture(scope="module")
def tools():
    return build_tools()


def test_expected_read_tools_are_all_exposed(tools):
    names = {t.name for t in tools}
    missing = EXPECTED_READ_TOOLS - names
    assert not missing, f"tools dropped from the conversation: {sorted(missing)}"


def test_expected_proposer_tools_are_all_exposed(tools):
    names = {t.name for t in tools}
    missing = EXPECTED_PROPOSER_TOOLS - names
    assert not missing, f"proposer tools dropped: {sorted(missing)}"


def test_every_exposed_tool_classifies_as_its_declared_tier(tools):
    """A tool's declared tier must match what the gate will compute for it."""
    mismatches = []
    for tool in tools:
        computed = classify(PlannedAction(tool=tool.name, params={}))
        if computed is not tool.tier:
            mismatches.append((tool.name, tool.tier.value, computed.value))
    assert not mismatches, (
        "declared tier disagrees with classify(): "
        f"{mismatches} (name, declared, computed)"
    )


def test_plan_and_enqueue_is_read_tier():
    """Regression: this was WRITE and silently dropped from the tool list."""
    assert classify(PlannedAction(tool="jarvis_plan_and_enqueue", params={})) is Tier.READ


def test_sandbox_and_browser_tools_are_read_tier():
    for name in (
        "sandbox_execute",
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_screenshot",
        "browser_extract",
    ):
        assert classify(PlannedAction(tool=name, params={})) is Tier.READ, name


def test_expected_local_write_tools_are_all_exposed(tools):
    names = {t.name for t in tools}
    missing = EXPECTED_LOCAL_WRITE_TOOLS - names
    assert not missing, f"local write tools dropped: {sorted(missing)}"


def test_no_exposed_tool_is_write_tier_without_being_a_proposer(tools):
    """Nothing WRITE-tier may reach the conversation unless it only proposes
    or is a local write that execute() runs directly."""
    allowed = EXPECTED_PROPOSER_TOOLS | EXPECTED_LOCAL_WRITE_TOOLS
    offenders = [
        t.name for t in tools
        if t.tier is Tier.WRITE and t.name not in allowed
    ]
    assert not offenders, f"write-tier tools exposed without a proposer path: {offenders}"
