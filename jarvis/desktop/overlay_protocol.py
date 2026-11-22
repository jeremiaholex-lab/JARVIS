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
    _action_from_snapshot,
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
from jarvis.security.file_edit import CONFIRM_PHRASE

HEARTBEAT_TIMEOUT_S = 5.0
# Descriptions are previews; the exact action stays in the approval snapshot.
_DISPLAY_KEYS = (
    "target_preview", "description", "text_preview", "keys_preview",
    "password_warning", "shot_preview",
)

_LOCK = threading.Lock()
_OVERLAY_TIMESTAMP = 0.0
_LATEST_CARD: dict[str, Any] | None = None
# Only the C# shell may arm the fail-closed gate. The web page announces its
# own connection separately, and must never be able to prove overlay presence.
_SHELL_EVENTS = ("shell_connected", "overlay_heartbeat")


def _now() -> float:
    return time.time()


def mark_desktop_overlay(event: str, timestamp: float | None = None) -> None:
    """Record a C# shell handshake/heartbeat event from the orb websocket.

    Page-only announcements such as "overlay_connected" and "connected" are
    ignored: any web script could send them, and they prove nothing about the
    native layer that would make an input visible to the operator.
    """
    global _OVERLAY_TIMESTAMP
    if event not in _SHELL_EVENTS:
        return
    with _LOCK:
        _OVERLAY_TIMESTAMP = _now() if timestamp is None else float(timestamp)


def is_shell_origin(msg: dict[str, Any]) -> bool:
    """True only for the C# shell's marked handshake/heartbeat ops.

    The WebView page and native shell share a WebSocket. A page script can
    emit arbitrary ops, so an explicit native-origin marker is required before
    an op is allowed to arm the fail-closed overlay gate.
    """
    if not isinstance(msg, dict):
        return False
    op = msg.get("op")
    return (op in _SHELL_EVENTS
            and str(msg.get("source") or "").casefold() == "shell")


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


def clear_desktop_card(reason: str = "new turn") -> None:
    """Forget the one-action card latch without cancelling this turn's proposal."""
    global _LATEST_CARD
    with _LOCK:
        _LATEST_CARD = None


def reset_desktop_session(reason: str = "reset") -> None:
    """Drop heartbeat, card and pending state (tests, shell disconnect)."""
    global _OVERLAY_TIMESTAMP
    clear_desktop_card(reason)
    with _LOCK:
        _OVERLAY_TIMESTAMP = 0.0
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
    """Produce the proposal-time event sequence for one action.

    The operator sees the confirmation card and a scanning marker while the
    proposal is open. Acting/done/hide are emitted only after a typed approval
    via :func:`desktop_cursor_result_events`; a card must not flash an "acting"
    cursor before the operator has approved it.
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
    ]


def desktop_cursor_result_events(action_id: str, status: str,
                                 reason: str = "") -> list[dict]:
    """Cursor settle + result after an approval attempt, never at proposal."""
    result: dict[str, Any] = {"op": "desktop_result", "action_id": action_id,
                              "status": status}
    if reason:
        # One shared redaction policy; no duplicate sensitive matching rules.
        result["reason"] = redact_sensitive(" ".join(str(reason).split()))
    return [
        {"op": "desktop_cursor_state", "action_id": action_id, "state": "acting"},
        {"op": "desktop_cursor_state", "action_id": action_id, "state": "done"},
        {"op": "desktop_cursor_hide", "action_id": action_id},
        result,
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


def adopt_pending_desktop_proposal(proposal: dict[str, Any],
                                   evidence: bytes = b"") -> dict[str, Any]:
    """Build the card for a proposal already registered by the agent tools.

    Task 7 registers desktop actions in the Task 1 store. This bridge reads
    that proposal's immutable snapshot instead of creating a second proposal,
    so the exact approved payload and the one-shot store are never duplicated.
    A dead overlay or an existing card still fails closed.
    """
    global _LATEST_CARD
    snapshot = proposal.get("snapshot") or {}
    action_id = str(proposal.get("action_id") or "")
    if (not desktop_overlay_connected() or is_cancelled()
            or latest_desktop_card() is not None):
        reason = ("overlay unavailable" if not desktop_overlay_connected()
                  else "stop requested" if is_cancelled()
                  else "one desktop action per request")
        return {
            "status": "blocked", "action_id": action_id,
            "snapshot": copy.deepcopy(snapshot), "reason": reason,
            "events": [{"op": "desktop_result", "action_id": action_id,
                        "status": "blocked", "reason": reason}],
        }
    try:
        action = _action_from_snapshot(snapshot)
    except (KeyError, TypeError, ValueError):
        return {
            "status": "blocked", "action_id": action_id,
            "snapshot": copy.deepcopy(snapshot), "reason": "invalid proposal",
            "events": [{"op": "desktop_result", "action_id": action_id,
                        "status": "blocked", "reason": "invalid proposal"}],
        }
    if action.action_id != action_id:
        return {
            "status": "blocked", "action_id": action_id,
            "snapshot": copy.deepcopy(snapshot), "reason": "invalid proposal",
            "events": [{"op": "desktop_result", "action_id": action_id,
                        "status": "blocked", "reason": "invalid proposal"}],
        }
    events = emit_desktop_action_events(action, evidence=evidence)
    card = {"status": "pending", "action_id": action.action_id,
            "snapshot": copy.deepcopy(snapshot), "events": events}
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


def match_desktop_confirmation(text: str,
                               card: dict[str, Any] | None) -> dict[str, Any] | None:
    """Map the card's typed phrase to an offline approval message.

    The chat host intercepts this before the model sees it: the model must not
    be able to answer its own confirmation prompt. A single pending card is
    required; any other text falls through to the normal turn.
    """
    if card is None or card.get("status") != "pending":
        return None
    normalized = " ".join(str(text or "").casefold().split())
    if normalized != CONFIRM_PHRASE.casefold():
        return None
    return {"action_id": card["action_id"], "approval": str(text),
            "snapshot": copy.deepcopy(card["snapshot"])}


def execute_desktop_confirmation(msg: dict[str, Any],
                                 control: DesktopControl) -> dict[str, Any]:
    """Execute one confirmed action through Task 5's full safety path.

    The executor re-checks the stop latch *after* consuming the one-shot
    approval, revalidates launch policy, and audits every outcome. The overlay
    gate is checked here as well: an approval arriving after the heartbeat dies
    must not reach the executor. Tests inject a fake control, so no test moves
    a real cursor or types real keys.
    """
    action_id = str(msg.get("action_id") or "")
    if not desktop_overlay_connected():
        _cancel_executor_pending("overlay lost before execution")
        return {"status": "blocked", "action_id": action_id,
                "reason": "overlay unavailable"}
    if is_cancelled():
        return {"status": "cancelled", "action_id": action_id,
                "reason": "stop requested"}

    from jarvis.desktop.executor import DesktopActionExecutor

    executor = DesktopActionExecutor(control)
    result = executor.execute(
        action_id,
        str(msg.get("approval") or msg.get("phrase") or ""),
        msg.get("snapshot") or {},
    )
    # One shared redaction policy for every UI/facts surface; the executor
    # already redacts its audit copy, but its status payload must be safe too.
    for key in ("reason", "error"):
        if key in result:
            result[key] = redact_sensitive(result[key])
    return result


__all__ = [
    "HEARTBEAT_TIMEOUT_S",
    "emit_desktop_action_events",
    "prepare_desktop_action",
    "mark_desktop_overlay",
    "desktop_overlay_connected",
    "desktop_heartbeat_status",
    "is_shell_origin",
    "clear_desktop_card",
    "adopt_pending_desktop_proposal",
    "match_desktop_confirmation",
    "desktop_cursor_result_events",
    "reset_desktop_session",
    "latest_desktop_card",
    "stop_desktop_action",
    "resolve_desktop_confirmation",
    "execute_desktop_confirmation",
]
