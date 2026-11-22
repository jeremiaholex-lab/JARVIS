"""Overlay protocol tests: one action, one event sequence, one confirmation.

Every test uses an in-memory action and fake evidence bytes. No real desktop
session starts, no OS input is injected, and no screenshot is uploaded.
"""
from __future__ import annotations

import base64
import json
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
    server.arm_native_shell_channel("test-shell-token", "test-relay-secret")
    server.mark_desktop_overlay("shell_connected", timestamp=time.time())
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
    server.mark_desktop_overlay("shell_connected")
    server.mark_desktop_overlay("page_connected")
    assert server.desktop_overlay_connected()  # prior native heartbeat still fresh
    server.mark_desktop_overlay("shell_connected", timestamp=time.time() - 10)
    assert not server.desktop_overlay_connected()
    server.mark_native_shell_heartbeat("shell_connected", "test-shell-token")
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
    card = server.adopt_pending_desktop_proposal(proposal, owner_id="owner-a")
    assert card["status"] == "pending"
    assert card["action_id"] == "prod1"
    assert card["snapshot"] == proposal["snapshot"]
    assert server.latest_desktop_card()["action_id"] == "prod1"


def test_adopted_card_survives_websocket_json_roundtrip():
    """Critical 1: card payloads are JSON-safe for the ws emit path."""
    import json

    proposal = create_desktop_action_proposal(_action("prod2"))
    card = server.adopt_pending_desktop_proposal(proposal, owner_id="owner-a")
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
    """Important 5: the old in-page token path is no longer an authority."""
    token = server.arm_native_shell_channel("shell-origin-test")
    assert server.is_shell_origin({"op": "shell_connected", "token": token})
    assert server.is_shell_origin({"op": "overlay_heartbeat", "token": token})
    assert not server.is_shell_origin({"op": "shell_connected"})
    assert not server.is_shell_origin({"op": "overlay_connected", "token": token})


# ---------------------------------------------------------------------------
# Fix round 2: unforgeable shell signals, reachable proposals, card ownership.

def test_page_source_field_cannot_arm_shell_gate():
    server.reset_desktop_session("spoofed page heartbeat")
    assert not server.is_shell_origin({"op": "overlay_heartbeat", "source": "shell"})
    server.mark_desktop_overlay("shell_connected", timestamp=time.time() - 10)
    assert not server.desktop_overlay_connected()
    token = server.arm_native_shell_channel("native-test-token")
    assert server.is_shell_origin({"op": "overlay_heartbeat", "token": token})
    server.mark_native_shell_heartbeat("overlay_heartbeat", "native-test-token")
    assert server.desktop_overlay_connected()


def test_proposal_hook_is_consumed_by_real_orb_turn():
    action = _action("hooked")
    proposal = create_desktop_action_proposal(action)
    q = _ProposalQueue()
    c = server.Client()
    c.desktop_control = FakeDesktop()
    # Task 7's tool returns the proposal as JSON text, not a dict.
    server.set_pending_desktop_proposal(json.dumps(proposal))
    import asyncio

    asyncio.run(server.emit_pending_desktop_proposal(c, q))
    ops = [json.loads(item)["op"] for item in q.items]
    assert "desktop_confirm" in ops
    assert "desktop_cursor_show" in ops
    assert "desktop_cursor_move" in ops
    assert server.latest_desktop_card()["action_id"] == "hooked"


class _ProposalQueue:
    def __init__(self):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)


def test_approval_is_bound_to_card_owner():
    from jarvis.desktop.actions import create_desktop_action_proposal

    action = _action("owned")
    proposal = create_desktop_action_proposal(action)
    server.adopt_pending_desktop_proposal(proposal, owner_id="owner-a")
    c = server.Client()
    c.desktop_control = FakeDesktop()
    verdict = server.match_desktop_confirmation(
        "yes, do it", server.latest_desktop_card(), client_id="owner-b")
    assert verdict is None
    assert server.latest_desktop_card()["owner_id"] == "owner-a"
    verdict = server.match_desktop_confirmation(
        "yes, do it", server.latest_desktop_card(), client_id="owner-a")
    assert verdict["action_id"] == "owned"


def test_native_relay_requires_backend_attestation():
    token = server.sign_native_relay(
        {"op": "desktop_cursor_hide", "action_id": "hooked"}, "relay-secret")
    assert server.verify_native_relay(
        {"op": "desktop_cursor_hide", "action_id": "hooked", "relay_sig": token},
        "relay-secret")
    assert not server.verify_native_relay(
        {"op": "desktop_cursor_hide", "action_id": "hooked", "relay_sig": token},
        "wrong-secret")
    assert not server.verify_native_relay(
        {"op": "desktop_cursor_hide", "action_id": "hooked"}, "relay-secret")


def test_backend_emitted_cursor_ops_carry_relay_signature():
    q = _ProposalQueue()
    c = server.Client()
    server.emit_signed(c, q, "desktop_cursor_hide", action_id="signed")
    msg = json.loads(q.items[0])
    assert server.verify_native_relay(msg, server.relay_secret())


def test_relay_secret_is_not_embedded_in_page_bootstrap():
    from pathlib import Path

    source = (Path(server.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8")
    assert "__JARVIS_RELAY_SECRET" not in source
    # The only injected task token is the pre-existing HTTP API token.
    app = server.build_app()
    assert not hasattr(app.state, "relay_secret")


# ---------------------------------------------------------------------------
# Fix round 2 (continued): every native overlay field is covered by the MAC.

def test_relay_signature_covers_cursor_coordinates_and_state():
    signed = server.sign_native_relay(
        {"op": "desktop_cursor_move", "action_id": "a1", "x": 10, "y": 20,
         "state": "scanning"}, "relay-secret")
    event = {"op": "desktop_cursor_move", "action_id": "a1", "x": 10, "y": 20,
             "state": "scanning", "relay_sig": signed}
    assert server.verify_native_relay(event, "relay-secret")
    for field, value in (("x", 999), ("y", 999), ("state", "acting")):
        tampered = dict(event, **{field: value})
        assert not server.verify_native_relay(tampered, "relay-secret")


def test_relay_signature_covers_stop_action_and_reason():
    signed = server.sign_native_relay(
        {"op": "desktop_hud_action", "action": "stop", "reason": "hud stop"},
        "relay-secret")
    event = {"op": "desktop_hud_action", "action": "stop", "reason": "hud stop",
             "relay_sig": signed}
    assert server.verify_native_relay(event, "relay-secret")
    assert not server.verify_native_relay(
        dict(event, action="inject"), "relay-secret")
    assert not server.verify_native_relay(
        dict(event, reason="forged"), "relay-secret")


def test_native_rejects_page_authored_ops_without_relay_signature():
    """Page-authored desktop_* ops cannot pass the native trust boundary."""
    import re
    from pathlib import Path

    base = Path(server.__file__).parent
    app_context = (base / "OrbAppContext.cs").read_text(encoding="utf-8")
    overlay = (base / "OverlayForm.cs").read_text(encoding="utf-8")
    orb = (base / "OrbForm.cs").read_text(encoding="utf-8")
    for source in (overlay, orb):
        assert "OrbAppContext.IsTrustedRelay" in source
    assert "if (!IsTrustedRelay(json)) return;" in app_context
    # The shell's self-stop must go through the signed backend round trip, not
    # a direct unsigned ApplyDesktopCursorCommand call.
    match = re.search(r"private void OnDesktopStop\(string reason\)\s*\{(.*?)\n    \}",
                      app_context, re.S)
    assert match and "_cursorOverlay?.ApplyDesktopCursorCommand" not in match.group(1)
    assert "PostOverlayBridge(\"desktop_hud_action\"" in match.group(1)


def test_native_heartbeat_never_enters_page_bridge():
    from pathlib import Path

    base = Path(server.__file__).parent
    app_context = (base / "OrbAppContext.cs").read_text(encoding="utf-8")
    orb = (base / "OrbForm.cs").read_text(encoding="utf-8")
    overlay = (base / "OverlayForm.cs").read_text(encoding="utf-8")
    page = (base / "static" / "index.html").read_text(encoding="utf-8")
    assert "ShellHeartbeatJson" not in app_context
    for source in (orb, overlay):
        assert "shell_connected" not in source
        assert "overlay_heartbeat" not in source
        assert "SendNativeHeartbeat" in source
    assert "heartbeats are authenticated by the native shell" in page
    assert "shell_connected" not in page
    assert "overlay_heartbeat" not in page


def test_attached_shell_channel_is_shared_out_of_band():
    """Attaching shells must not generate secrets that differ from the backend."""
    from pathlib import Path

    shell_host = (Path(server.__file__).parent / "ShellHost.cs").read_text(
        encoding="utf-8")
    assert "File.Exists(SecretFilePath)" in shell_host
    assert "JARVIS_SHELL_TOKEN" in shell_host
    assert "JARVIS_RELAY_SECRET" in shell_host
    # The file lives under LocalApplicationData, not in the served page folder.
    assert "Environment.SpecialFolder.LocalApplicationData" in shell_host
    # Both sides use the same path and file shape.
    assert "orb-desktop-channel.json" in Path(server.__file__).read_text(
        encoding="utf-8")


# ---------------------------------------------------------------------------
# Fix round 3: native heartbeat channel and live-session channel binding.

def test_page_bridge_cannot_arm_native_heartbeat_gate():
    from fastapi.testclient import TestClient

    # Simulate a page script that has taken over window.jarvisHud.bridge and
    # sends every op/token combination it can see through the WebSocket.
    server.reset_desktop_session("page bridge spoof")
    app = server.build_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        with client.websocket_connect("/ws") as ws:
            for op in ("shell_connected", "overlay_heartbeat"):
                ws.send_json({"op": op, "source": "shell"})
                ws.send_json({"op": op, "token": "test-shell-token"})
    # The retired ops are ignored. No page-visible op may arm the latch, and a
    # WebSocket receive here would block because they emit no response.
    assert not server.desktop_overlay_connected()


def test_authenticated_native_channel_arms_heartbeat_without_page_ops():
    from fastapi.testclient import TestClient

    server.reset_desktop_session("native channel heartbeat")
    app = server.build_app()
    server.arm_native_shell_channel("test-shell-token", "test-relay-secret")
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        response = client.post("/api/native/heartbeat", json={
            "op": "overlay_heartbeat",
            "nonce": "native-nonce-1",
        }, headers={"x-jarvis-shell-token": "test-shell-token",
                    "x-jarvis-shell-hmac": server.sign_native_heartbeat(
                        "overlay_heartbeat", "native-nonce-1", "test-shell-token")})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "ok"
    assert server.desktop_overlay_connected()

    assert not server.desktop_overlay_connected(time.time() + 10)
    server.reset_desktop_session("native channel wrong mac")
    assert not server.desktop_overlay_connected()
    with TestClient(app, client=("127.0.0.1", 12346)) as client:
        response = client.post("/api/native/heartbeat", json={
            "op": "overlay_heartbeat",
            "nonce": "native-nonce-2",
        }, headers={"x-jarvis-shell-token": "test-shell-token",
                    "x-jarvis-shell-hmac": "0" * 64})
        assert response.status_code == 401
    assert not server.desktop_overlay_connected()


def test_stale_native_channel_file_is_not_accepted_when_backend_restarts(
        monkeypatch, tmp_path):
    from pathlib import Path

    stale_channel = tmp_path / "orb-desktop-channel.json"
    stale_channel.write_text(json.dumps({
        "session_id": "old-backend-session",
        "shell_token": "a" * 64,
        "relay_secret": "b" * 64,
    }), encoding="utf-8")
    monkeypatch.setattr(server, "_CHANNEL_FILE", stale_channel)
    monkeypatch.delenv("JARVIS_SHELL_TOKEN", raising=False)
    monkeypatch.delenv("JARVIS_RELAY_SECRET", raising=False)
    live_session, relay_secret, shell_token = server.establish_native_channel()
    assert live_session != "old-backend-session"
    assert shell_token != "a" * 64
    assert relay_secret != "b" * 64
    saved = json.loads(stale_channel.read_text(encoding="utf-8"))
    assert saved["session_id"] == live_session
    assert saved["shell_token"] == shell_token
    assert saved["relay_secret"] == relay_secret
    assert server.verify_native_channel_session(
        live_session, server.shell_token(), server.relay_secret())
    assert not server.verify_native_channel_session("old-backend-session",
                                                    "a" * 64, "b" * 64)


def test_native_session_challenge_uses_caller_nonce():
    """A stale/unsigned caller must not borrow a server-generated nonce."""
    from fastapi.testclient import TestClient

    app = server.build_app()
    with TestClient(app, client=("127.0.0.1", 12348)) as client:
        nonce = "caller-generated-nonce"
        response = client.post("/api/native/session", json={"nonce": nonce},
                               headers={
                                   "x-jarvis-shell-token": "test-shell-token",
                                   "x-jarvis-shell-hmac": server.sign_native_heartbeat(
                                       "native_session", nonce, "test-shell-token"),
                               })
        assert response.status_code == 200, response.text
        assert response.json()["session_id"] == server.native_session_id()
        assert "nonce" not in response.json()
        assert "nonce_sig" not in response.json()

        bad = client.post("/api/native/session", json={"nonce": nonce},
                          headers={"x-jarvis-shell-token": "test-shell-token",
                                   "x-jarvis-shell-hmac": "0" * 64})
        assert bad.status_code == 401


def test_attach_rejects_stale_channel_before_using_its_secrets(monkeypatch):
    from pathlib import Path

    shell_host = (Path(server.__file__).parent / "ShellHost.cs").read_text(
        encoding="utf-8")
    assert "ValidateNativeChannel" in shell_host
    assert "native/session" in shell_host
    assert "NativeChannelError" in shell_host

    app = server.build_app()
    from fastapi.testclient import TestClient

    with TestClient(app, client=("127.0.0.1", 12347)) as client:
        response = client.post("/api/native/heartbeat", json={
            "op": "overlay_heartbeat", "nonce": "stale-session",
        }, headers={"x-jarvis-shell-token": "a" * 64,
                    "x-jarvis-shell-hmac": server.sign_native_heartbeat(
                        "overlay_heartbeat", "stale-session", "a" * 64)})
        assert response.status_code == 401
