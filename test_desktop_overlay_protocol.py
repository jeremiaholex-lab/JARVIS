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
    cancel_pending_desktop_actions,
    configure_desktop_audit,
    create_desktop_action_proposal,
    redact_sensitive,
    resolve_desktop_action,
)
from jarvis.desktop import executor as executor_module

import server


@pytest.fixture(autouse=True)
def _clean_state():
    cancel_pending_desktop_actions("overlay test setup")
    configure_desktop_audit(None)
    executor_module.reset_cancel()
    server.reset_desktop_session("overlay test setup")
    server.mark_desktop_overlay("connected")
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
        "desktop_cursor_state",
        "desktop_cursor_state",
        "desktop_cursor_hide",
        "desktop_result",
    ]
    assert ops.count("desktop_hud_action") == 0  # HUD Stop, not approval
    assert ops.index("desktop_confirm") == 0
    assert ops[-1] == "desktop_result"
    assert events[0]["action_id"] == action.action_id
    assert events[0]["snapshot"]["target"]["x"] == 10
    assert events[-1]["status"] in ("pending", "blocked")
    assert [e["state"] for e in events if e["op"] == "desktop_cursor_state"] == [
        "scanning", "acting", "done",
    ]


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
    server.mark_desktop_overlay("heartbeat", timestamp=time.time() - 10)
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
    server.mark_desktop_overlay("connected")
    assert server.desktop_overlay_connected()
    assert server.desktop_heartbeat_status()["status"] == "ok"
    server.mark_desktop_overlay("heartbeat", timestamp=time.time() - 10)
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
