"""Host-agnostic write executor: propose -> confirm -> execute + audit.

Stage 2 opens a *write* surface (the local project/deadline store) without
touching the framework. A write tool the model can propose is side-effect
free on its own: calling it only registers a pending write and hands the
model a confirmation token. The real mutation happens later, in
``resolve()``, only after the user confirms — and every decision (approved
or rejected) is recorded to the same hash-chained AuditLogger the console
ConfirmGate uses, so the audit trail is one chain across both hosts.

Why a token instead of blocking on input(): the brain and the orb have no
stdin. A chat host (orb UI, later Telegram) surfaces the pending write and
resolves the token when the user taps confirm. The console's ConfirmGate
keeps its stdin behaviour; they share the auditor, the tier policy and the
audit chain, just not the prompt transport.
"""
from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .auditor import Auditor, Decision, Verdict
from .tiers import PlannedAction, Tier, classify

# The one write surface opened in stage 2: the local project/deadline store.
# Kept as a module constant so the tool layer and the executor agree on the
# param shape without one importing the other.
WRITE_TOOL = "jarvis_projects_write"
_VALID_ACTIONS = ("add_project", "add_deadline", "set_deadline_status")


@dataclass(slots=True)
class PendingWrite:
    """A proposed-but-unconfirmed write, held in the executor until resolved."""

    token: str
    action: PlannedAction
    verdict: Verdict
    state: str = "pending"  # pending | approved | rejected

    def public(self) -> Dict[str, Any]:
        """UI-safe view of a pending write (no engine internals)."""
        return {
            "token": self.token,
            "action": self.action.tool,
            "params": dict(self.action.params),
            "tier": self.verdict.tier.value,
            "auditor": self.verdict.decision.value,
            "reason": self.verdict.reason,
            "state": self.state,
            "summary": _write_summary(dict(self.action.params)),
        }


def _write_summary(params: Dict[str, Any]) -> str:
    action = str(params.get("action", "?"))
    if action == "add_project":
        return f"add project {params.get('name')!r} at {params.get('path')!r}"
    if action == "add_deadline":
        return (f"add deadline {params.get('title')!r} for "
                f"{params.get('project_name')!r} due {params.get('due_date')!r}")
    if action == "set_deadline_status":
        return (f"mark deadline {params.get('deadline_id')!r} "
                f"{params.get('status')!r}")
    return f"{action} {params}"


class SecuredWrites:
    """Propose a write, hold it, and execute it only on confirmation.

    Thread-safe: the orb can hold a turn while a write is pending and another
    surface lists the pending set. Execution itself goes through the injected
    store; the auditor and audit logger are optional but the tier check is not.
    """

    def __init__(self, store, auditor: Auditor, audit_logger=None, *, use_llm: bool = False) -> None:
        self._store = store
        self._auditor = auditor
        self._audit = audit_logger
        self._use_llm = use_llm
        self._pending: Dict[str, PendingWrite] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Propose: side-effect free. Registers a pending write, returns the token.
    # ------------------------------------------------------------------
    def propose(self, params: Dict[str, Any]) -> Dict[str, Any]:
        params = _coerce_params(params)
        error = _validate(params)
        if error:
            # Bad shape: no pending write, just tell the model what it sent.
            return {"pending": False, "error": error}

        action = PlannedAction(tool=WRITE_TOOL, params=params)
        verdict = self._auditor.judge(action)
        # A HOLD/FLAG verdict still yields a token — the host surfaces the
        # reason and the user can override a HOLD. Destructive is a hard gate
        # that the host must render with its typed-phrase UI (out of scope
        # for the projects surface, but the token already carries the tier).
        token = "pw_" + secrets.token_hex(8)
        with self._lock:
            self._pending[token] = PendingWrite(token, action, verdict)
        return {
            "pending": True,
            "token": token,
            "summary": _write_summary(params),
            "tier": verdict.tier.value,
            "auditor": verdict.decision.value,
            "reason": verdict.reason,
            "note": "This will not run until the user confirms it. Tell them it is "
                    "waiting on their confirmation, do not assume it happened.",
        }

    # ------------------------------------------------------------------
    # The open confirmations a host can show, and the result of resolving one.
    # ------------------------------------------------------------------
    def pending(self) -> list[Dict[str, Any]]:
        with self._lock:
            return [p.public() for p in self._pending.values() if p.state == "pending"]

    def resolve(self, token: str, approved: bool) -> Dict[str, Any]:
        """Approve or reject a pending write. Only an approval mutates state.

        This is the one place in the whole write path that touches the store,
        so it is also the one place the audit chain records the decision.
        """
        with self._lock:
            pw = self._pending.get(token)
            if pw is None:
                return {"error": f"unknown write token {token!r}"}
            if pw.state != "pending":
                return {"error": f"write {token} is already {pw.state}", "token": token}
            pw.state = "approved" if approved else "rejected"

        if approved:
            outcome = self._execute(pw)
        else:
            outcome = {"skipped": True}

        self._record(pw, approved, outcome)
        return {"token": token, "approved": approved, "outcome": outcome}

    # ------------------------------------------------------------------
    def _execute(self, pw: PendingWrite) -> Dict[str, Any]:
        params = dict(pw.action.params)
        store = self._store
        action = params.get("action")
        try:
            if action == "add_project":
                pid = store.add_project(params["name"], params["path"])
                return {"created_project_id": pid, "action": action}
            if action == "add_deadline":
                project = _find_project(store, params["project_name"])
                if project is None:
                    return {"error": f"unknown project {params['project_name']!r}; add it first"}
                did = store.add_deadline(project["id"], params["title"], params["due_date"])
                return {"created_deadline_id": did, "project": project["name"], "action": action}
            if action == "set_deadline_status":
                ok = store.update_deadline_status(int(params["deadline_id"]), params["status"])
                return {"updated": ok, "action": action} if ok else {"error": "no such deadline", "action": action}
            return {"error": f"unsupported action {action!r}"}
        except Exception as exc:
            return {"error": f"{exc.__class__.__name__}: {exc}"}

    def _record(self, pw: PendingWrite, approved: bool, outcome: Dict[str, Any]) -> None:
        if self._audit is None:
            return
        from openjarvis.security.types import SecurityEvent, SecurityEventType

        import time as _time

        event_type = (
            SecurityEventType.TOOL_EXECUTED if approved
            else SecurityEventType.TOOL_BLOCKED
        )
        preview = (
            f"secured write tool={pw.action.tool} tier={pw.verdict.tier.value} "
            f"auditor={pw.verdict.decision.value} approved={approved} :: "
            f"{_write_summary(dict(pw.action.params))} {outcome.get('error', '')}"
        )
        event = SecurityEvent(
            event_type=event_type,
            timestamp=_time.time(),
            content_preview=preview,
            action_taken=("approved" if approved else "denied"),
        )
        try:
            self._audit.log(event)
        except Exception:
            # Auditing must never block the write decision.
            pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _coerce_params(arguments: Any) -> Dict[str, Any]:
    if isinstance(arguments, str):
        import json

        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return {"_bad": arguments}
    return arguments if isinstance(arguments, dict) else {}


def _validate(params: Dict[str, Any]) -> Optional[str]:
    if params.get("_bad"):
        return "arguments are not a JSON object"
    action = params.get("action")
    if action not in _VALID_ACTIONS:
        return f"action must be one of {', '.join(_VALID_ACTIONS)}"
    if action == "add_project":
        if not params.get("name") or not params.get("path"):
            return "add_project needs 'name' and 'path'"
    if action == "add_deadline":
        missing = [k for k in ("project_name", "title", "due_date") if not params.get(k)]
        if missing:
            return f"add_deadline needs {', '.join(missing)}"
    if action == "set_deadline_status":
        if params.get("status") not in ("open", "completed", "cancelled"):
            return "set_deadline_status needs status in open/completed/cancelled"
        if params.get("deadline_id") in (None, ""):
            return "set_deadline_status needs 'deadline_id'"
    return None


def _find_project(store, name: str) -> Optional[Dict[str, Any]]:
    want = str(name).casefold()
    for p in store.list_projects():
        if str(p.get("name", "")).casefold() == want:
            return p
    return None


__all__ = ["SecuredWrites", "PendingWrite", "WRITE_TOOL", "_VALID_ACTIONS"]
