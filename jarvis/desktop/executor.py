"""Executor: perform exactly one approved desktop action.

Wires the Task 1 confirmation store, the Task 3 control boundary, and the
Task 4 launch policy into one safe execution path. The executor performs a
single action per user request: it re-validates the typed approval, re-checks
the launch policy immediately before execution, and fails closed on
cancellation. Status/audit events flow through Task 1's audit trail; no
overlay or heartbeat protocol is emitted here (that is Task 6's territory).
"""
from __future__ import annotations

import os
import sys
import threading
from typing import Any, Dict, Optional

from jarvis.desktop.actions import (
    DesktopAction,
    _audit as _audit_event,
    cancel_pending_desktop_actions as _cancel_pending_store,
    redact_sensitive,
    resolve_desktop_action,
)
from jarvis.desktop.targets import validate_launch_target
from jarvis.desktop.win32_control import DesktopControl

__all__ = [
    "DesktopActionExecutor",
    "cancel_pending_desktop_actions",
    "is_cancelled",
    "reset_cancel",
]

# Process-wide stop latch. A stop source (HUD stop, hotkey, new user turn)
# arms it; the host clears it for a new request via reset_cancel(). The
# pending store and the latch settle together, in one call.
_LOCK = threading.Lock()
_cancelled = False


def cancel_pending_desktop_actions(reason: str) -> None:
    """Cancel every pending desktop action and arm the executor stop latch.

    This is the executor's front door to the Task 1 cancel path: the shared
    pending store, the audit trail, and the process-wide latch all settle in
    one call. Idempotent.
    """
    global _cancelled
    _cancel_pending_store(reason)
    with _LOCK:
        _cancelled = True


def is_cancelled() -> bool:
    """True once any stop source cancelled the pending desktop action."""
    with _LOCK:
        return _cancelled


def reset_cancel() -> None:
    """Host hook: a new user request may proceed despite an earlier stop.

    A stop cancels the *previous* pending action; it must not freeze every
    future turn, so the host clears the latch when a new request begins.
    """
    global _cancelled
    with _LOCK:
        _cancelled = False


def _audit(event: str, action: DesktopAction, detail: str = "") -> None:
    """Share Task 1's audit trail, redacting any detail that names a
    sensitive window or carries a secret — the trail must stay UI-safe."""
    _audit_event(event, action, redact_sensitive(_one_line(detail)))


def _one_line(text: str) -> str:
    """Audit payloads stay single-line so content_preview parses cleanly."""
    return " ".join(str(text or "").split())


class DesktopActionExecutor:
    """Performs exactly one approved desktop action with cancellation support.

    Construction is side-effect free; every OS effect flows through the
    injected ``control`` (Task 3 protocol). Tests inject fakes, so no real
    cursor ever moves.
    """

    def __init__(self, control: DesktopControl) -> None:
        self.control = control

    # ------------------------------------------------------------- lifecycle

    def cancel(self, reason: str) -> None:
        """Cancel this host's pending desktop action and arm the stop latch."""
        cancel_pending_desktop_actions(reason)

    def is_cancelled(self) -> bool:  # noqa: A003 - the brief names this method
        """True after cancel(reason) or a process-wide stop (new user turn)."""
        return is_cancelled()

    # --------------------------------------------------------------- execute

    def execute(
        self,
        action_id: str,
        approval: str,
        snapshot: dict,
    ) -> Dict[str, Any]:
        """Resolve, revalidate, and execute one approved desktop action.

        The only success status is ``"ok"``. Every cancellation, rejection,
        failure, and execution is audited through Task 1's audit trail.
        """
        # Cancellation gate before resolution: a stop request beats any
        # in-flight approval.
        if self.is_cancelled():
            _audit(
                "cancelled",
                _action_from_id_or_snapshot(action_id, snapshot),
                "stop requested before resolution",
            )
            return {
                "status": "cancelled",
                "action_id": action_id,
                "reason": "stop requested",
            }

        # One-shot resolution: a stale replay, a spent card, a bad phrase, or
        # a snapshot mismatch returns None — no OS action may be inferred from
        # an approval alone.
        action: Optional[DesktopAction] = resolve_desktop_action(
            action_id, approval, snapshot
        )
        if action is None:
            # Every failure is audited, including "never proposed" and
            # "already spent" — a client replaying a consumed id N times
            # must not vanish from the trail. Task 1 audits its own
            # "denied"/"expired" paths; the not-found and already-settled
            # paths settle here, reconstructed from the snapshot.
            _audit(
                "failed",
                _action_from_id_or_snapshot(action_id, snapshot),
                "proposal not found or already consumed",
            )
            return {
                "status": "failed",
                "action_id": action_id,
                "error": "proposal not found, spent, expired, or rejected",
            }

        # TOCTOU closure: re-validate the launch policy immediately before
        # execution. A launch target that no longer passes fails closed,
        # before any OS effect.
        if action.kind in ("open_app", "open_file"):
            path = str(
                action.target.get("name_or_path") or action.target.get("path") or ""
            )
            allowed, why = validate_launch_target(path)
            if not allowed:
                # Task 4 reasons are fixed English strings; they never echo
                # the target or a secret.
                _audit("rejected", action, f"launch policy revalidation: {why}")
                return {
                    "status": "failed",
                    "action_id": action.action_id,
                    "error": f"launch policy revalidation rejected target: {why}",
                }

        # Cancellation gate before execution (second check): nothing that
        # happens between approval and input may run.
        if self.is_cancelled():
            _audit("cancelled", action, "stop requested before execution")
            return {
                "status": "cancelled",
                "action_id": action.action_id,
                "reason": "stop requested",
            }

        # Execute exactly one action through the injected control boundary.
        try:
            _execute_action(self.control, action)
        except Exception as exc:  # noqa: BLE001 - every OS fault fails closed
            err = _one_line(exc)
            _audit("failed", action, err)
            return {
                "status": "failed",
                "action_id": action.action_id,
                "error": err,
            }

        _audit("executed", action)
        return {"status": "ok", "action_id": action.action_id, "kind": action.kind}


def _action_from_id_or_snapshot(action_id: str, snapshot: dict) -> DesktopAction:
    """Audit stand-in when the pending store already settled the action."""
    try:
        return DesktopAction(
            action_id=str(snapshot.get("action_id") or action_id),
            kind=str(snapshot.get("kind", "")),
            target=dict(snapshot.get("target") or {}),
            risk=str(snapshot.get("risk", "")),
            evidence=dict(snapshot.get("evidence") or {}),
            expires_at=float(snapshot.get("expires_at") or 0.0),
        )
    except Exception:
        return DesktopAction(action_id, "", {}, "", {}, 0.0)


def _open_target(control: Any, path: str) -> None:
    """Launch through the control's open hook, falling back to the OS shell.

    The Task 3 protocol is input-only (8 methods); launch is the one OS
    effect it does not cover. A host may provide an ``open`` hook on its
    control (tests always do); otherwise the Windows shell opens the target.
    """
    open_fn = getattr(control, "open", None)
    if open_fn is not None:
        open_fn(path)
        return
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        raise OSError(
            "launch actions require a Windows host or a control with an open hook"
        )


def _execute_action(control: Any, action: DesktopAction) -> None:
    """Route one approved action to the control boundary (Task 3 protocol)."""
    kind = action.kind
    target = action.target

    if kind == "move_cursor":
        control.move_cursor(int(target.get("x", 0)), int(target.get("y", 0)))
    elif kind == "click":
        x = int(target.get("x", 0))
        y = int(target.get("y", 0))
        button = str(target.get("button", "left"))
        clicks = int(target.get("clicks", 1))
        control.move_cursor(x, y)
        control.click(x, y, button, clicks)
    elif kind == "scroll":
        x = int(target.get("x", 0))
        y = int(target.get("y", 0))
        direction = str(target.get("direction", "down"))
        amount = int(target.get("amount", 1))
        control.scroll(x, y, direction, amount)
    elif kind == "type":
        text = str(target.get("text", ""))
        x = target.get("x")
        y = target.get("y")
        if x is not None and y is not None:
            control.move_cursor(int(x), int(y))
        control.type_text(text)
    elif kind == "press_keys":
        # Key codes may arrive as int virtual-key values; normalize to the
        # string form the control boundary expects.
        keys = [str(k) for k in target.get("keys", [])]
        x = target.get("x")
        y = target.get("y")
        if x is not None and y is not None:
            control.move_cursor(int(x), int(y))
        control.press_keys(keys)
    elif kind in ("open_app", "open_file"):
        path = str(target.get("name_or_path") or target.get("path") or "")
        if not path:
            raise ValueError("launch target is empty")
        _open_target(control, path)
    else:
        raise ValueError(f"unsupported action kind: {kind}")
