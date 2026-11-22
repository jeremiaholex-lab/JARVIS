"""Overlay protocol for confirmed desktop actions.

This module owns the wire contract between the orb, the confirmation card, and
the C# cursor window. It is deliberately free of WebView2, screen capture, and
OS input: everything here is pure data plus a small in-memory heartbeat latch.

Safety invariants:
  * a card is produced only when the shell overlay is connected and fresh;
  * the card displays ``evidence`` previews only — ``target`` is execution-only
    and travels inside the exact approval ``snapshot``;
  * a crop is local PNG bytes rendered as a data URI (never a URL or a path);
  * every action flows through the same fixed event sequence so the operator
    sees the cursor synchronise before input happens;
  * stops are one-shot and settle the pending store and the executor latch.
"""
from __future__ import annotations

import base64
import copy
import threading
import time
from typing import Any

from jarvis.desktop.actions import (
    DesktopAction,
    _snapshot_of,
    cancel_pending_desktop_actions as _cancel_pending,
    create_desktop_action_proposal,
    redact_sensitive,
    resolve_desktop_action,
)
from jarvis.desktop.executor import (
    cancel_pending_desktop_actions as _cancel_executor_pending,
    is_cancelled,
    reset_cancel,
)
from jarvis.desktop.win32_control import DesktopControl

HEARTBEAT_TIMEOUT_S = 5.0
# Descriptions are previews; the exact action stays in the approval snapshot.
_DISPLAY_KEYS = (
    "target_preview", "description", "text_preview", "keys_preview",
    "password_warning", "shot_preview",
)

_LOCK = threading.Lock()
_OVERLAY_TIMESTAMP = 0.0
_LATEST_CARD: dict[str, Any] | None = None


def _now() -> float:
    return time.time()


def mark_desktop_overlay(event: str, timestamp: float | None = None) -> None:
    """Record a shell handshake/heartbeat event from the orb websocket.

    Any recognised event proves the shell overlay is alive at ``timestamp``.
    Unknown events are ignored so a stray client cannot keep a dead shell
    artificially connected.
    """
    global _OVERLAY_TIMESTAMP
    if event not in ("connected", "heartbeat"):
        return
    with _LOCK:
        _OVERLAY_TIMESTAMP = _now() if timestamp is None else float(timestamp)


def desktop_overlay_connected(now: float | None = None) -> bool:
    """True only while a shell heartbeat is present and within its deadline."""
    with _LOCK:
        stamp = _OVERLAY_TIMESTAMP
    if stamp <= 0:
        return False
    return (now if now is not None else _now()) - stamp < HEARTBEAT_TIMEOUT_S


def desktop_heartbeat_status(now: float | None = None) -> dict[str, Any]:
    """UI-safe status for the connection indicator."""
    with _LOCK:
        stamp = _OVERLAY_TIMESTAMP
    if stamp <= 0:
        return {"status": "offline", "age_s": None}
    age = max(0.0, (now if now is not None else _now()) - stamp)
    return {
        "status": "ok" if age < HEARTBEAT_TIMEOUT_S else "stale",
        "age_s": round(age, 2),
        "timeout_s": HEARTBEAT_TIMEOUT_S,
    }


def reset_desktop_session(reason: str = "reset") -> None:
    """Drop heartbeat, card and pending state (tests, shell disconnect)."""
    global _OVERLAY_TIMESTAMP, _LATEST_CARD
    with _LOCK:
        _OVERLAY_TIMESTAMP = 0.0
        _LATEST_CARD = None
    _cancel_executor_pending(reason)
    reset_cancel()


def latest_desktop_card() -> dict[str, Any] | None:
    with _LOCK:
        return copy.deepcopy(_LATEST_CARD)


def _crop_uri(evidence: bytes) -> str:
    """Local PNG bytes only; never a file path or network URL."""
    if not evidence:
        return ""
    return "data:image/png;base64," + base64.b64encode(bytes(evidence)).decode("ascii")


def _safe_display(action: DesktopAction) -> dict[str, Any]:
    """UI copy from evidence only, redacted through the shared policy."""
    display: dict[str, Any] = {}
    for key in _DISPLAY_KEYS:
        if key not in action.evidence:
            continue
        value = action.evidence[key]
        display[key] = redact_sensitive(value) if isinstance(value, str) else copy.deepcopy(value)
    return display


def emit_desktop_action_events(action: DesktopAction, evidence: bytes = b"") -> list[dict]:
    """Produce the fixed, ordered overlay event sequence for one action.

    The sequence is: confirmation card, cursor reveal, target move, scanning,
    acting, done, hide, and result. The card carries the exact snapshot, a
    redacted preview, typed-phrase requirement and a local crop data URI.
    """
    target = action.target or {}
    x = target.get("x")
    y = target.get("y")
    snapshot = _snapshot_of(action)
    card = {
        "action_id": action.action_id,
        "action": action.action_id,
        "kind": action.kind,
        "risk": action.risk,
        "snapshot": snapshot,
        "evidence": copy.deepcopy(action.evidence),
        "display": _safe_display(action),
        "crop": _crop_uri(evidence),
        "requires_phrase": True,
        "expires_at": action.expires_at,
    }
    return [
        {"op": "desktop_confirm", **card},
        {"op": "desktop_cursor_show", "action_id": action.action_id,
         "kind": action.kind, "state": "scanning"},
        {"op": "desktop_cursor_move", "action_id": action.action_id,
         "x": x, "y": y},
        {"op": "desktop_cursor_state", "action_id": action.action_id,
         "state": "scanning"},
        {"op": "desktop_cursor_state", "action_id": action.action_id,
         "state": "acting"},
        {"op": "desktop_cursor_state", "action_id": action.action_id,
         "state": "done"},
        {"op": "desktop_cursor_hide", "action_id": action.action_id},
        {"op": "desktop_result", "action_id": action.action_id,
         "status": "pending"},
    ]


def prepare_desktop_action(action: DesktopAction, overlay_connected: bool,
                           evidence: bytes = b"") -> dict[str, Any]:
    """Gate one proposal on a live overlay and return the card to emit.

    ``overlay_connected`` must be supplied by the caller from the heartbeat
    latch. A missing overlay never registers a proposal and yields only a
    blocked result event, so an invisible OS input cannot happen.
    """
    global _LATEST_CARD
    snapshot = _snapshot_of(action)
    if (not overlay_connected or not desktop_overlay_connected()
            or is_cancelled() or not isinstance(action, DesktopAction)):
        return {
            "status": "blocked",
            "action_id": action.action_id,
            "snapshot": snapshot,
            "reason": "overlay unavailable",
            "events": [
                {"op": "desktop_result", "action_id": action.action_id,
                 "status": "blocked", "reason": "overlay unavailable"}
            ],
        }
    # One action per user request: the host clears the latch on a new turn.
    if latest_desktop_card() is not None:
        return {
            "status": "blocked",
            "action_id": action.action_id,
            "snapshot": snapshot,
            "reason": "one desktop action per request",
            "events": [
                {"op": "desktop_result", "action_id": action.action_id,
                 "status": "blocked", "reason": "one desktop action per request"}
            ],
        }

    events = emit_desktop_action_events(action, evidence=evidence)
    proposal = create_desktop_action_proposal(action)  # side-effect: pending store
    card = {
        "status": "pending",
        "action_id": action.action_id,
        "snapshot": proposal["snapshot"],
        "events": events,
    }
    with _LOCK:
        _LATEST_CARD = copy.deepcopy(card)
    return card


def stop_desktop_action(reason: str = "hud stop") -> list[dict]:
    """Stop the pending action: cancel both gates and emit the stop sequence."""
    global _LATEST_CARD
    card = latest_desktop_card()
    action_id = card["action_id"] if card else ""
    _cancel_executor_pending(reason)
    with _LOCK:
        _LATEST_CARD = None
    return [
        {"op": "desktop_hud_action", "action": "stop",
         "action_id": action_id, "reason": reason},
        {"op": "desktop_cursor_state", "action_id": action_id, "state": "stopped"},
        {"op": "desktop_cursor_hide", "action_id": action_id},
        {"op": "desktop_result", "action_id": action_id,
         "status": "cancelled", "reason": reason},
    ]


def resolve_desktop_confirmation(msg: dict[str, Any]) -> dict[str, Any]:
    """Resolve a typed approval against the exact card snapshot, once.

    A bad phrase or snapshot spends the proposal (one-shot) and yields a
    failed result; nothing is executed and no action object is returned.
    """
    global _LATEST_CARD
    action_id = str(msg.get("action_id") or "")
    approval = str(msg.get("approval") or msg.get("phrase") or "")
    snapshot = msg.get("snapshot") or {}
    resolved = resolve_desktop_action(action_id, approval, snapshot)
    if resolved is None:
        return {
            "status": "failed",
            "action_id": action_id,
            "error": "proposal not found, spent, expired, or rejected",
        }
    # The overlay must still be alive at the moment input would happen.
    if not desktop_overlay_connected():
        _cancel_executor_pending("overlay lost before execution")
        return {
            "status": "blocked",
            "action_id": action_id,
            "reason": "overlay unavailable",
        }
    with _LOCK:
        _LATEST_CARD = None
    return {"status": "ok", "action_id": action_id, "action": resolved}


def execute_desktop_confirmation(msg: dict[str, Any],
                                 control: DesktopControl) -> dict[str, Any]:
    """Resolve and execute one confirmed action through the injected control.

    Used by the orb websocket host. The action comes from the exact snapshot
    settled by the typed approval; no input happens on a bad phrase, a stale
    overlay, or a cancellation. Tests inject a fake control, so this never
    moves a real cursor or types real keys.
    """
    resolution = resolve_desktop_confirmation(msg)
    if resolution["status"] != "ok":
        return {k: v for k, v in resolution.items() if k != "action"}
    action = resolution["action"]
    try:
        from jarvis.desktop.executor import _execute_action

        _execute_action(control, action)
    except Exception as exc:
        return {
            "status": "failed",
            "action_id": action.action_id,
            "error": " ".join(str(exc).split()),
        }
    return {"status": "ok", "action_id": action.action_id, "kind": action.kind}


__all__ = [
    "HEARTBEAT_TIMEOUT_S",
    "emit_desktop_action_events",
    "prepare_desktop_action",
    "mark_desktop_overlay",
    "desktop_overlay_connected",
    "desktop_heartbeat_status",
    "reset_desktop_session",
    "latest_desktop_card",
    "stop_desktop_action",
    "resolve_desktop_confirmation",
    "execute_desktop_confirmation",
]
