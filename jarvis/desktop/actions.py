"""Confirmation contract for proposed desktop actions.

Every action is two-phase: a proposal is side-effect free, and the only thing
that can settle it is a typed ``yes, do it`` plus an exact snapshot match.
This module never calls an OS API — execution belongs to a later task.
"""
from __future__ import annotations

import copy
import hmac
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from jarvis.security.file_edit import CONFIRM_PHRASE

# Sensitive window title/process patterns: password managers, banking, private
# browsing. Matching is case-insensitive substring matching, so a title such as
# "1Password vault" is redacted in full rather than partially masked.
SENSITIVE_PATTERNS: tuple[str, ...] = (
    "password",
    "1password",
    "keepass",
    "bitwarden",
    "lastpass",
    "vault",
    "banking",
    "bank of ",
    "incognito",
    "private browsing",
    "private window",
    "inprivate",
    "tor browser",
)

_REDACTED = "[REDACTED]"


def redact_sensitive(value: str) -> str:
    """Return a UI/facts safe string; sensitive contexts are replaced wholly."""
    text = str(value or "")
    lowered = text.casefold()
    if any(pattern in lowered for pattern in SENSITIVE_PATTERNS):
        return _REDACTED
    return text


@dataclass(slots=True)
class DesktopAction:
    """One fully specified, proposed desktop action (no OS side effects)."""

    action_id: str
    kind: str
    target: dict
    risk: str
    evidence: dict
    expires_at: float


@dataclass(slots=True)
class _PendingAction:
    action: DesktopAction
    snapshot: dict
    state: str = "pending"  # pending | approved | denied | expired | cancelled


# Module-level store: the orb and the executor must see the same proposal.
_PENDING: Dict[str, _PendingAction] = {}
_LOCK = threading.Lock()
_AUDIT: Any = None
# The orb reads this hook once per turn. It is set by create_desktop_action_proposal
# so Task 7's tools only need the ordinary Task 1 proposal call to surface a card.
_LATEST_PROPOSAL: Optional[dict] = None


def consume_desktop_proposal() -> Optional[dict]:
    """Take the newest proposal from this turn, once. No proposal is invented."""
    global _LATEST_PROPOSAL
    with _LOCK:
        proposal = _LATEST_PROPOSAL
        _LATEST_PROPOSAL = None
    return copy.deepcopy(proposal)


def _set_desktop_proposal_hook(proposal: dict) -> None:
    """Set the hook for a proposal that was registered through the Task 1 API."""
    global _LATEST_PROPOSAL
    with _LOCK:
        _LATEST_PROPOSAL = copy.deepcopy(proposal)


def clear_desktop_proposal_hook() -> None:
    """Forget an unconsumed hook when a new user turn begins."""
    global _LATEST_PROPOSAL
    with _LOCK:
        _LATEST_PROPOSAL = None


def configure_desktop_audit(logger: Any) -> None:
    """Inject the project's AuditLogger (same chain the confirm gate uses)."""
    global _AUDIT
    _AUDIT = logger


def _audit(event: str, action: DesktopAction, detail: str = "") -> None:
    """Audit every transition, best effort: logging cannot block safety."""
    logger = _AUDIT
    if logger is None:
        return
    try:
        from openjarvis.security.types import SecurityEvent, SecurityEventType
    except ImportError:
        from types import SimpleNamespace

        # The vendored OpenJarvis submodule is absent in the test worktree.
        # Keep the same event shape so audit hooks and tests still see every
        # transition; production hosts inject the real AuditLogger and types.
        SecurityEvent = lambda **kwargs: SimpleNamespace(**kwargs)
        SecurityEventType = SimpleNamespace(
            TOOL_EXECUTED="tool_executed", TOOL_BLOCKED="tool_blocked"
        )

    event_type = (
        SecurityEventType.TOOL_BLOCKED
        if event in ("denied", "expired", "cancelled", "rejected")
        else SecurityEventType.TOOL_EXECUTED
    )
    try:
        logger.log(SecurityEvent(
            event_type=event_type,
            timestamp=time.time(),
            content_preview=(
                f"desktop action event={event} id={action.action_id} "
                f"kind={action.kind} risk={action.risk} :: {detail}"
            ),
            action_taken=event,
        ))
    except Exception:
        # Mirrors confirm.py / file_edit.py: an audit failure must never change
        # the outcome of a safety decision.
        pass


def _action_from_snapshot(snapshot: dict) -> DesktopAction:
    """Rebuild the authoritative action from the approved snapshot only.

    The card, the equality check, and the executor must all see one immutable
    payload. Never hand back the caller's live object after proposal time.
    """
    return DesktopAction(
        action_id=snapshot["action_id"],
        kind=snapshot["kind"],
        target=copy.deepcopy(snapshot["target"]),
        risk=snapshot["risk"],
        evidence=copy.deepcopy(snapshot["evidence"]),
        expires_at=snapshot["expires_at"],
    )


def _snapshot_of(action: DesktopAction) -> dict:
    """The exact approved payload: any extra/missing/changed field fails closed."""
    return {
        "action_id": action.action_id,
        "kind": action.kind,
        "target": copy.deepcopy(action.target),
        "risk": action.risk,
        "evidence": copy.deepcopy(action.evidence),
        "expires_at": action.expires_at,
    }


def _phrase_ok(approval: str) -> bool:
    """All three words, in order, in any punctuation/spacing/case."""
    words = re.sub(r"[^a-z ]", " ", str(approval or "").lower()).split()
    return tuple(words) == ("yes", "do", "it")


def create_desktop_action_proposal(action: DesktopAction) -> dict:
    """Register a pending proposal and return the card payload. Never executes."""
    global _LATEST_PROPOSAL
    snapshot = _snapshot_of(action)
    authoritative = _action_from_snapshot(snapshot)
    with _LOCK:
        _PENDING[action.action_id] = _PendingAction(authoritative, snapshot)
    _audit("proposed", authoritative, f"phrase={CONFIRM_PHRASE}")
    proposal = {
        "status": "pending",
        "action_id": authoritative.action_id,
        "action": authoritative.action_id,
        "kind": authoritative.kind,
        "target": copy.deepcopy(authoritative.target),
        "risk": authoritative.risk,
        "evidence": copy.deepcopy(authoritative.evidence),
        "expires_at": authoritative.expires_at,
        "snapshot": copy.deepcopy(snapshot),
        "requires_phrase": True,
        "note": (
            "NOTHING has been done. The user must type "
            f"'{CONFIRM_PHRASE}' in the confirmation card."
        ),
    }
    with _LOCK:
        _LATEST_PROPOSAL = copy.deepcopy(proposal)
    return proposal


_UI_PHASES = frozenset(("", "scanning", "queued", "acting", "done", "stopped"))


def sign_native_relay(payload: dict, secret: str) -> str:
    """MAC for backend-to-shell cursor ops; the page cannot mint this value.

    Every field the native overlay uses is covered: op, action id, status,
    stop action, reason, coordinates, and cursor state. A page script that
    modifies any of them invalidates the signature.
    """
    body = "|".join(
        str(payload.get(key, "") if payload.get(key) is not None else "")
        for key in ("op", "action_id", "status", "action", "reason",
                    "x", "y", "state", "kind")
    )
    return hmac.new(str(secret).encode(), body.encode(), "sha256").hexdigest()


def verify_native_relay(msg: dict, secret: str) -> bool:
    """Constant-time check that a cursor op came through the backend."""
    if not isinstance(msg, dict) or not isinstance(secret, str) or not secret:
        return False
    op = msg.get("op")
    if not isinstance(op, str) or not op.startswith("desktop_"):
        return False
    # Restrict the signed surface to scalar fields the native overlay uses.
    # Complex card fields (snapshot, crop, evidence) never drive the cursor and
    # must not be relayed to it.
    signed = {key: msg.get(key, "") for key in (
        "op", "action_id", "status", "action", "reason",
        "x", "y", "state", "kind")}
    if any(isinstance(v, (dict, list)) for v in signed.values()):
        return False
    if str(signed.get("state") or "") not in _UI_PHASES:
        return False
    supplied = msg.get("relay_sig")
    return (isinstance(supplied, str)
            and hmac.compare_digest(supplied, sign_native_relay(signed, secret)))


def resolve_desktop_action(action_id: str, approval: str, snapshot: dict) -> Optional[DesktopAction]:
    """Consume a proposal on typed approval; return a deep copy exactly once.

    Success, denial, expiry and snapshot mismatch all invalidate the proposal.
    A stale replay, an extra field, a missing field, or a changed value returns
    None — no OS action may ever be inferred from an approval alone.
    """
    with _LOCK:
        pending = _PENDING.get(action_id)
        if pending is None or pending.state != "pending":
            return None
        if time.time() >= pending.action.expires_at:
            pending.state = "expired"
            expired = pending.action
        else:
            expired = None
        if expired is not None:
            # One-shot invalidation: an expired proposal can never be approved.
            _PENDING.pop(action_id, None)
        else:
            if not _phrase_ok(approval) or snapshot != pending.snapshot:
                pending.state = "denied"
                _audit("denied", pending.action, "phrase or snapshot mismatch")
                return None
            pending.state = "approved"
            # One-shot: no retry, no second execution, no late revalidation.
            _PENDING.pop(action_id, None)
            # Reconstruct from the authoritative snapshot so the executor's
            # payload is byte-for-byte what the approver saw on the card.
            approved = _action_from_snapshot(pending.snapshot)
    if expired is not None:
        _audit("expired", expired, "approval window elapsed")
        return None
    _audit("approved", approved, "typed phrase and snapshot matched")
    return approved


def cancel_pending_desktop_actions(reason: str) -> None:
    """Cancel every pending proposal (HUD Stop, hotkey, new turn, failure)."""
    with _LOCK:
        cancelled = list(_PENDING.values())
        _PENDING.clear()
    for pending in cancelled:
        pending.state = "cancelled"
        _audit("cancelled", pending.action, f"reason={reason}")


__all__ = [
    "CONFIRM_PHRASE",
    "SENSITIVE_PATTERNS",
    "DesktopAction",
    "redact_sensitive",
    "create_desktop_action_proposal",
    "consume_desktop_proposal",
    "clear_desktop_proposal_hook",
    "_set_desktop_proposal_hook",
    "sign_native_relay",
    "verify_native_relay",
    "resolve_desktop_action",
    "cancel_pending_desktop_actions",
    "configure_desktop_audit",
]
