"""Overlay protocol tests: one action, one event sequence, one confirmation.

Every test uses an in-memory action and fake evidence bytes. No real desktop
session starts, no OS input is injected, and no screenshot is uploaded.
"""
from __future__ import annotations

import base64
import time

import pytest

from jarvis.desktop.actions import (
    DesktopAction,
    SENSITIVE_PATTERNS,
    cancel_pending_desktop_actions,
    configure_desktop_audit,
    create_desktop_action_proposal,
    redact_sensitive,
    resolve_desktop_action,
)
from jarvis.desktop import executor as executor_module
from jarvis.desktop.executor import DesktopActionExecutor

import server


@pytest.fixture(autouse=True)
def _clean_state():
    cancel_pending_desktop_actions("overlay test setup")
    configure_desktop_audit(None)
    executor_module.reset_cancel()
    server.reset_desktop_session("overlay test setup")
    server.mark_desktop_overlay("shell_connected")
    yield
    server.reset_desktop_session("overlay test teardown")
    cancel_pending_desktop_actions("overlay test teardown")
    configure_desktop_audit(None)
    executor_module.reset_cancel()


def _action(action_id="ov1", kind="click", target=None, evidence=None):
    return DesktopAction(
        action_id,
        kind,
        target if target is not None else {
            "x": 10, "y": 20, "button": "left", "clicks": 1,
        },
        "write",
        evidence if evidence is not None else {"target_preview": "harmless target"},
        time.time() + 60,
    )


def test_cursor_overlay_receives_approved_coordinates():
    events = server.emit_desktop_action_events(_action(), evidence=b"png")
    moved = [e for e in events if e["op"] == "desktop_cursor_move"]
    assert moved[0]["x"] == 10 and moved[0]["y"] == 20


def test_overlay_absence_fails_closed():
    assert server.prepare_desktop_action(
        _action(), overlay_connected=False)["status"] == "blocked"


def test_event_sequence_covers_confirmation_cursor_hud_and_result():
    action = _action()
    events = server.emit_desktop_action_events(action, evidence=b"png")
    ops = [e["op"] for e in events]
    assert ops == [
        "desktop_confirm",
        "desktop_cursor_show",
        "desktop_cursor_move",
        "desktop_cursor_state",
    ]
    assert ops.count("desktop_hud_action") == 0  # HUD Stop, not approval
    assert ops.index("desktop_confirm") == 0
    assert ops[-1] == "desktop_cursor_state"
    assert events[0]["action_id"] == action.action_id
    assert events[0]["snapshot"]["target"]["x"] == 10
    assert events[0]["requires_phrase"] is True
    assert [e["state"] for e in events if e["op"] == "desktop_cursor_state"] == [
        "scanning",
    ]  # acting/done wait for approval


def test_card_shows_safe_preview_only_and_carries_exact_snapshot():
    action = _action(evidence={
        "target_preview": "Notepad (safe)",
        "text_preview": "hello",
        "description": "Click [REDACTED]",
    })
    card = server.prepare_desktop_action(action, overlay_connected=True)
    confirm = card["events"][0]
    assert confirm["op"] == "desktop_confirm"
    assert confirm["evidence"]["target_preview"] == "Notepad (safe)"
    assert json_roundtrip(confirm)["snapshot"]["target"]["x"] == 10
    # Execution target stays in the snapshot; it is never card display text.
    assert "target" not in confirm["display"]
    assert not any("10" in str(v) for v in confirm["display"].values())


def json_roundtrip(event):
    import json
    return json.loads(json.dumps(event))


def test_sensitive_preview_is_redacted_and_never_displayed_raw():
    action = _action(evidence={"target_preview": "1Password vault"})
    card = server.prepare_desktop_action(action, overlay_connected=True)
    preview = card["events"][0]["display"].get("target_preview", "")
    assert preview == "[REDACTED]"
    assert redact_sensitive("1Password vault") == preview


def test_evidence_png_is_local_data_uri_and_never_a_network_url():
    png = b"\x89PNG\r\n\x1a\n" + b"card-crop"
    events = server.emit_desktop_action_events(_action(), evidence=png)
    confirm = events[0]
    assert confirm["crop"].startswith("data:image/png;base64,")
    assert base64.b64decode(confirm["crop"].split(",", 1)[1]) == png
    assert "http" not in confirm["crop"]


def test_blocked_preparation_never_registers_an_approval():
    action = _action()
    server.mark_desktop_overlay("overlay_heartbeat", timestamp=time.time() - 10)
    card = server.prepare_desktop_action(action, overlay_connected=False)
    assert card["status"] == "blocked"
    assert resolve_desktop_action(action.action_id, "yes, do it",
                                  card["snapshot"]) is None
    assert [e["op"] for e in card["events"]] == ["desktop_result"]


def test_connected_preparation_registers_one_card_and_tracks_heartbeat():
    card = server.prepare_desktop_action(_action(), overlay_connected=True)
    assert card["status"] == "pending"
    assert server.desktop_overlay_connected()
    assert card["snapshot"]["action_id"] == "ov1"
    # A second proposal in the same request is refused: one action per request.
    blocked = server.prepare_desktop_action(
        _action(action_id="ov2"), overlay_connected=True)
    assert blocked["status"] == "blocked"


def test_overlay_handshake_and_stale_heartbeat_fail_closed():
    server.mark_desktop_overlay("shell_connected")
    assert server.desktop_overlay_connected()
    assert server.desktop_heartbeat_status()["status"] == "ok"
    server.mark_desktop_overlay("overlay_heartbeat", timestamp=time.time() - 10)
    assert not server.desktop_overlay_connected()
    assert server.desktop_heartbeat_status()["status"] == "stale"


def test_hud_stop_cancels_pending_action_and_emits_hud_event():
    action = _action()
    card = server.prepare_desktop_action(action, overlay_connected=True)
    events = server.stop_desktop_action("hud stop")
    assert {"op": "desktop_hud_action", "action": "stop"} in [
        {"op": e["op"], "action": e.get("action")} for e in events]
    assert resolve_desktop_action(action.action_id, "yes, do it",
                                  card["snapshot"]) is None
    assert executor_module.is_cancelled()


def test_desktop_confirm_approval_preserves_exact_snapshot():
    action = _action()
    card = server.prepare_desktop_action(action, overlay_connected=True)
    result = server.resolve_desktop_confirmation(
        {"action_id": action.action_id, "approval": "yes, do it",
         "snapshot": card["snapshot"]})
    assert result["status"] == "ok"
    assert result["action"].target == action.target


def test_desktop_confirm_rejects_wrong_phrase_without_execution():
    action = _action()
    card = server.prepare_desktop_action(action, overlay_connected=True)
    result = server.resolve_desktop_confirmation(
        {"action_id": action.action_id, "approval": "yes", "snapshot": card["snapshot"]})
    assert result["status"] == "failed"
    assert "action" not in result


# ---------------------------------------------------------------------------
# Fix round 1: safety-critical RED tests. No real control, desktop, or shell.

class FakeDesktop:
    """Records every input call; the overlay protocol never uses a real OS."""

    def __init__(self):
        self.calls = []

    def move_cursor(self, x, y):
        self.calls.append(("move_cursor", x, y))

    def click(self, x, y, button, clicks):
        self.calls.append(("click", x, y, button, clicks))

    def scroll(self, x, y, direction, amount):
        self.calls.append(("scroll", x, y, direction, amount))

    def type_text(self, text):
        self.calls.append(("type_text", text))

    def press_keys(self, keys):
        self.calls.append(("press_keys", list(keys)))

    def open(self, path):
        self.calls.append(("open", path))


class FakeAudit:
    """Captures audit events without touching the project's audit chain."""

    def __init__(self):
        self.events = []

    def log(self, event):
        self.events.append(event)


def _approval(card, phrase="yes, do it"):
    return {"action_id": card["action_id"], "approval": phrase,
            "snapshot": card["snapshot"]}


def test_execute_desktop_confirmation_routes_through_executor_and_audits():
    """Critical 2 / Important 8: one approved action, full Task 5 gates."""
    action = _action()
    card = server.prepare_desktop_action(action, overlay_connected=True)
    control, audit = FakeDesktop(), FakeAudit()
    configure_desktop_audit(audit)
    result = server.execute_desktop_confirmation(_approval(card), control)
    assert result["status"] == "ok"
    assert control.calls == [("move_cursor", 10, 20), ("click", 10, 20, "left", 1)]
    assert [e.action_taken for e in audit.events][-1] == "executed"


def test_stop_after_approval_blocks_input():
    """Critical 2 / Important 8: HUD Stop wins before execution."""
    action = _action()
    card = server.prepare_desktop_action(action, overlay_connected=True)
    msg = _approval(card)
    server.resolve_desktop_confirmation(msg)
    server.stop_desktop_action("hud stop after approval")
    control, audit = FakeDesktop(), FakeAudit()
    configure_desktop_audit(audit)
    result = server.execute_desktop_confirmation(msg, control)
    assert result["status"] == "cancelled"
    assert control.calls == []


def test_overlay_loss_at_execution_blocks_input():
    """Critical 2 / Important 8: a dead overlay never sees injected input."""
    card = server.prepare_desktop_action(_action(), overlay_connected=True)
    server.mark_desktop_overlay("overlay_heartbeat", timestamp=time.time() - 10)
    control = FakeDesktop()
    result = server.execute_desktop_confirmation(_approval(card), control)
    assert result["status"] == "blocked"
    assert control.calls == []


def test_launch_policy_is_revalidated_before_execution():
    """Critical 2 / Important 8: a changed policy still blocks launch."""
    action = _action(kind="open_file", target={"path": "C:/tmp/report.pdf"})
    card = server.prepare_desktop_action(action, overlay_connected=True)
    control = FakeDesktop()
    import jarvis.desktop.executor as executor_impl

    original = executor_impl.validate_launch_target

    def reject(path):
        return False, "Target rejected by test policy."

    executor_impl.validate_launch_target = reject
    try:
        result = server.execute_desktop_confirmation(_approval(card), control)
    finally:
        executor_impl.validate_launch_target = original
    assert result["status"] == "failed"
    assert "Target rejected by test policy." in result["error"]
    assert control.calls == []


def test_error_details_are_redacted_with_shared_policy():
    """Critical 3: execution errors cannot echo a sensitive raw path."""
    card = server.prepare_desktop_action(_action(), overlay_connected=True)
    control = FakeDesktop()

    def fail(x, y):
        raise OSError("C:/Banking/statement.pdf is locked")

    control.move_cursor = fail
    result = server.execute_desktop_confirmation(_approval(card), control)
    assert "C:/Banking" not in result["error"]
    assert result["error"] == redact_sensitive(result["error"])


def test_card_lifecycle_waits_for_approval():
    """Important 4: scanning marker stays visible; input states come later."""
    card = server.prepare_desktop_action(_action(), overlay_connected=True)
    ops = [e["op"] for e in card["events"]]
    assert ops == ["desktop_confirm", "desktop_cursor_show",
                   "desktop_cursor_move", "desktop_cursor_state"]
    assert card["events"][-1]["state"] == "scanning"
    lifecycle = server.desktop_cursor_result_events("ov1", "ok")
    assert [e["op"] for e in lifecycle] == [
        "desktop_cursor_state", "desktop_cursor_state", "desktop_cursor_hide",
        "desktop_result"]
    assert [e.get("state") for e in lifecycle[:2]] == ["acting", "done"]
    assert lifecycle[-1]["status"] == "ok"


def test_page_announcement_cannot_arm_shell_overlay_gate():
    """Important 5: only C# shell_connected/heartbeats arm the fail-closed gate."""
    server.reset_desktop_session("page cannot arm gate")
    server.mark_desktop_overlay("page_connected")
    assert not server.desktop_overlay_connected()
    server.mark_desktop_overlay("shell_connected")
    assert server.desktop_overlay_connected()


def test_new_turn_clears_card_latch():
    """Important 6: a stale card cannot block every later request."""
    server.prepare_desktop_action(_action("ov1"), overlay_connected=True)
    assert server.latest_desktop_card() is not None
    server.clear_desktop_card("new turn")
    assert server.latest_desktop_card() is None
    second = server.prepare_desktop_action(_action("ov2"), overlay_connected=True)
    assert second["status"] == "pending"


def test_production_proposal_reaches_card_without_new_proposal():
    """Critical 1: the orb bridges the Task 1 store into the card/WS pump."""
    action = _action("prod1")
    proposal = create_desktop_action_proposal(action)
    card = server.adopt_pending_desktop_proposal(proposal)
    assert card["status"] == "pending"
    assert card["action_id"] == "prod1"
    assert card["snapshot"] == proposal["snapshot"]
    assert server.latest_desktop_card()["action_id"] == "prod1"


def test_adopted_card_survives_websocket_json_roundtrip():
    """Critical 1: card payloads are JSON-safe for the ws emit path."""
    import json

    proposal = create_desktop_action_proposal(_action("prod2"))
    card = server.adopt_pending_desktop_proposal(proposal)
    decoded = json.loads(json.dumps(card))
    assert decoded["events"][0]["op"] == "desktop_confirm"
    assert decoded["events"][0]["snapshot"] == proposal["snapshot"]


def test_typed_approval_in_chat_is_routed_to_desktop_confirmation():
    """Critical 1: the card's typed phrase reaches the exact snapshot, offline."""
    card = server.prepare_desktop_action(_action("chat1"), overlay_connected=True)
    verdict = server.match_desktop_confirmation("yes, do it", server.latest_desktop_card())
    assert verdict == {"action_id": "chat1", "approval": "yes, do it",
                       "snapshot": card["snapshot"]}
    assert server.match_desktop_confirmation("yes", server.latest_desktop_card()) is None


def test_shell_origin_marker_is_required_for_heartbeats():
    """Important 5: an injected shell op carries proof the page cannot forge."""
    assert server.is_shell_origin({"op": "shell_connected", "source": "shell"})
    assert server.is_shell_origin({"op": "overlay_heartbeat", "source": "SHELL"})
    assert not server.is_shell_origin({"op": "shell_connected"})
    assert not server.is_shell_origin({"op": "overlay_connected", "source": "shell"})
