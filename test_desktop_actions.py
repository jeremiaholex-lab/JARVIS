"""Confirmation contract tests for proposed desktop actions.

These pin the safety boundary: a proposal never executes, an approval must be
typed and exact, and no approval can settle the same action twice.
"""
from __future__ import annotations

import time

import pytest

from jarvis.agent import tools as agent_tools
from jarvis.desktop.actions import (
    DesktopAction,
    cancel_pending_desktop_actions,
    configure_desktop_audit,
    create_desktop_action_proposal,
    redact_sensitive,
    resolve_desktop_action,
)
from jarvis.security.tiers import PlannedAction, Tier, classify


@pytest.fixture(autouse=True)
def clear_pending():
    cancel_pending_desktop_actions("test setup")
    yield
    cancel_pending_desktop_actions("test teardown")


def _action(action_id="a1", expires_at=None):
    return DesktopAction(
        action_id,
        "click",
        {"x": 10, "y": 20},
        "write",
        {"description": "Click a harmless target"},
        expires_at if expires_at is not None else time.time() + 60,
    )


def _snapshot(action):
    return {
        "action_id": action.action_id,
        "kind": action.kind,
        "target": dict(action.target),
        "risk": action.risk,
        "evidence": dict(action.evidence),
        "expires_at": action.expires_at,
    }


def test_proposal_does_not_execute_and_is_immutable():
    action = DesktopAction("a1", "click", {"x": 10, "y": 20}, "write", {}, 1.0)
    proposal = create_desktop_action_proposal(action)
    assert proposal["status"] == "pending"
    action.target["x"] = 999
    assert proposal["target"]["x"] == 10


def test_stale_or_modified_approval_is_rejected():
    action = DesktopAction("a1", "click", {"x": 10, "y": 20}, "write", {}, 1.0)
    create_desktop_action_proposal(action)
    assert resolve_desktop_action("a1", "yes, do it", {"x": 999, "y": 20}) is None
    assert resolve_desktop_action("a1", "no", {"x": 10, "y": 20}) is None


def test_redacts_sensitive_windows_from_facts_and_targets():
    assert redact_sensitive("1Password vault") == "[REDACTED]"
    assert redact_sensitive("Chrome Incognito") == "[REDACTED]"
    assert redact_sensitive("Visual Studio Code") == "Visual Studio Code"


def test_exact_approval_returns_a_copy_once():
    action = _action()
    proposal = create_desktop_action_proposal(action)
    resolved = resolve_desktop_action("a1", "YES, DO IT", proposal["snapshot"])
    assert resolved is not None
    assert resolved.target == {"x": 10, "y": 20}
    assert resolve_desktop_action("a1", "yes, do it", proposal["snapshot"]) is None

    resolved.target["x"] = 777
    assert create_desktop_action_proposal(_action(action_id="a2"))["target"]["x"] == 10


def test_extra_or_missing_snapshot_fields_fail_closed():
    action = _action()
    snapshot = create_desktop_action_proposal(action)["snapshot"]
    assert resolve_desktop_action("a1", "yes, do it", {**snapshot, "extra": 1}) is None
    assert resolve_desktop_action("a1", "yes, do it", {
        key: value for key, value in snapshot.items() if key != "risk"
    }) is None


def test_expired_proposal_cannot_be_approved():
    action = _action(expires_at=time.time() - 1)
    snapshot = create_desktop_action_proposal(action)["snapshot"]
    assert resolve_desktop_action("a1", "yes, do it", snapshot) is None


def test_cancel_invalidates_all_pending_actions():
    action = _action()
    snapshot = create_desktop_action_proposal(action)["snapshot"]
    cancel_pending_desktop_actions("hud stop")
    assert resolve_desktop_action("a1", "yes, do it", snapshot) is None


def test_expiry_is_audited_once_and_invalidates_the_proposal():
    events = []

    class FakeAudit:
        def log(self, event):
            events.append(event)

    configure_desktop_audit(FakeAudit())
    try:
        expired = _action(action_id="expired-1", expires_at=time.time() - 1)
        snapshot = create_desktop_action_proposal(expired)["snapshot"]
        assert resolve_desktop_action("expired-1", "yes, do it", snapshot) is None
        assert resolve_desktop_action("expired-1", "yes, do it", snapshot) is None
    finally:
        configure_desktop_audit(None)

    kinds = [e.content_preview.split(" event=")[1].split(" ")[0] for e in events]
    assert kinds == ["proposed", "expired"]


def test_every_transition_is_audited():
    events = []

    class FakeAudit:
        def log(self, event):
            events.append(event)

    configure_desktop_audit(FakeAudit())
    try:
        action = _action(action_id="audit-1")
        snapshot = create_desktop_action_proposal(action)["snapshot"]
        resolve_desktop_action("audit-1", "yes, do it", snapshot)
        denied = _action(action_id="audit-2")
        denied_snapshot = create_desktop_action_proposal(denied)["snapshot"]
        resolve_desktop_action("audit-2", "no", denied_snapshot)
        create_desktop_action_proposal(_action(action_id="audit-3"))
        cancel_pending_desktop_actions("new turn")
    finally:
        configure_desktop_audit(None)

    seen = [e.content_preview for e in events]
    kinds = [s.split(" event=")[1].split(" ")[0] for s in seen]
    expected = [
        "proposed", "approved", "proposed", "denied", "proposed", "cancelled"
    ]
    assert kinds[:6] == expected, kinds
    assert set(kinds) == set(expected), kinds


@pytest.mark.parametrize("name", (
    "desktop_open_app", "desktop_open_file", "desktop_move_cursor",
    "desktop_click", "desktop_scroll", "desktop_type", "desktop_press_keys",
))
def test_desktop_tools_are_non_read_proposers(name):
    tools = agent_tools.build_tools()
    tool = next(t for t in tools if t.name == name)
    assert tool.tier is Tier.DESTRUCTIVE
    assert classify(PlannedAction(tool=name)) is Tier.DESTRUCTIVE
    assert name in agent_tools._PROPOSE_ONLY_TOOLS


def test_desktop_click_tool_returns_a_pending_proposal():
    tools = agent_tools.build_tools()
    import json

    result = json.loads(agent_tools.execute(tools, "desktop_click", {"x": 10, "y": 20}))
    assert result["status"] == "pending"
    assert result["target"]["x"] == 10
    assert result["requires_phrase"] is True
