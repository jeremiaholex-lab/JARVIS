"""Stage 3: host-confirmed *file* edits — the DESTRUCTIVE gate, made real.

The stage-2 write surface queues project-store rows and a single tap settles
it. Editing a source file is a different risk class: the result is executable
text, a one-character slip is a broken build, and the model writing the edit
is the same model that might be talked into it by a web page. So this surface
adds three things on top of the stage-2 pattern:

  * a DESTRUCTIVE tier (tiers.classify), settled by a TYPED phrase, not a tap;
  * a unified diff computed at *propose* time, so the human approves the exact
    change — not a description of it;
  * a stale check: if the file changed between propose and confirm, the edit
    is refused rather than clobbering someone else's write.

Like stage 2 it is a proposer: ``propose()`` never touches the file. Only
``resolve(token, approved, confirm_phrase)`` writes, and only when the phrase
matches. Enforcement lives here, in the executor, so no host UI can weaken it
by forgetting to ask — a host that shows a one-tap button still gets refused.

Guards (all deterministic, all checked before a token is even issued):
  * credential-shaped paths are refused outright (the owner's .env and keys
    are edited by hand, never by a model that has read a web page);
  * VCS / runtime-state / dependency directories are refused;
  * binaries and oversized files are refused;
  * `create` never overwrites, `rewrite` may but shows the whole diff.
"""
from __future__ import annotations

import difflib
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .auditor import Auditor, Decision, Verdict
from .tiers import PlannedAction, Tier, is_sensitive

EDIT_TOOL = "jarvis_file_edit"
CONFIRM_PHRASE = "yes, do it"

# The one confirmation grammar. Both hosts (orb, Telegram) import these so the
# regexes cannot drift apart — the drift was a real bug: Telegram captured any
# token where the orb captured only fe_, so a phrase aimed at a write token was
# routed to the edit resolver. Keep them here, beside the phrase they enforce.
_PHRASE_RE_SRC = r"yes,?\s+do\s+it"
EDIT_CONFIRM_RE = re.compile(rf"^({_PHRASE_RE_SRC})\s+(fe_\w+)$", re.IGNORECASE)
EDIT_CANCEL_RE = re.compile(r"^(no|cancel)\s+(fe_\w+)$", re.IGNORECASE)
# "yes fe_x": not enough to apply a destructive edit, but must be answered
# offline — a model would claim it had applied the file.
EDIT_BARE_YES_RE = re.compile(r"^yes\s+(fe_\w+)$", re.IGNORECASE)
# "yes, do it pw_x": the phrase is for edits, the token is not an edit token.
EDIT_PHRASE_MISPLACED_RE = re.compile(rf"^({_PHRASE_RE_SRC})\s+(\S+)$", re.IGNORECASE)
# Write tokens (one-tap): "yes pw_x" / "no pw_x" / "cancel pw_x".
WRITE_CONFIRM_RE = re.compile(r"^(yes|no|cancel)\s+(\S+)$", re.IGNORECASE)

_VALID_ACTIONS = ("replace", "rewrite", "create")


def _norm_token(token: str) -> str:
    """Tokens are minted lowercase by secrets.token_hex; match them that way.

    Confirmation patterns carry re.IGNORECASE so a shouted phrase still works,
    but that flag also applies to the token they capture, so "YES, DO IT FE_X"
    yields an uppercase token. Looking it up verbatim would report an unknown
    token for one that is plainly pending, so normalise here — once, at the
    executor, rather than in each host.
    """
    return (token or "").strip().lower()


_MAX_EDIT_BYTES = 200_000
_MAX_DIFF_CHARS = 4000
_REFUSED_PARTS = {".git", ".hg", ".svn", ".openjarvis", "node_modules", "__pycache__", ".venv"}

_PHRASE_TOKENS = ("yes", "do", "it")


@dataclass(slots=True)
class PendingEdit:
    """A proposed-but-unapplied file edit, held until resolved."""

    token: str
    kind: str                 # replace | rewrite | create
    action: PlannedAction
    verdict: Verdict
    path: Path
    before: str
    after: str
    diff: str
    state: str = "pending"    # pending | applied | rejected | stale

    def public(self) -> Dict[str, Any]:
        """UI-safe view: the diff is the point, the file bodies are not."""
        summary = _summary(self.kind, self.path, self.before, self.after)
        return {
            "token": self.token,
            "action": self.action.tool,
            "kind": self.kind,
            "params": {"action": self.kind, "path": str(self.path)},
            "tier": self.verdict.tier.value,
            "auditor": self.verdict.decision.value,
            "reason": _reason(self.verdict),
            "state": self.state,
            "summary": summary,
            "diff": self.diff[:_MAX_DIFF_CHARS],
            "diff_truncated": len(self.diff) > _MAX_DIFF_CHARS,
            "requires_phrase": True,
        }


class SecuredFileEdits:
    """Propose a file edit, show its diff, apply it only on typed confirmation."""

    def __init__(self, auditor: Auditor, audit_logger=None) -> None:
        self._auditor = auditor
        self._audit = audit_logger
        self._pending: Dict[str, PendingEdit] = {}
        self._applied: Dict[str, PendingEdit] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Propose: reads the file, builds the diff, touches nothing.
    # ------------------------------------------------------------------
    def propose(self, params: Dict[str, Any]) -> Dict[str, Any]:
        params = _coerce_params(params)
        prepared, error = self._prepare(params)
        if error:
            return {"pending": False, "error": error}

        kind, path, before, after = prepared
        action = PlannedAction(tool=EDIT_TOOL, params={"action": kind, "path": str(path)})
        verdict = self._auditor.judge(action)
        if verdict.decision is Decision.FLAG:
            # The deterministic critic never FLAGs a single clean action; if a
            # future critic does, refuse rather than queue something ambiguous.
            return {"pending": False, "error": f"auditor flagged this edit: {verdict.reason}"}

        diff = _unified_diff(before, after, str(path))
        token = "fe_" + secrets.token_hex(8)
        pe = PendingEdit(token, kind, action, verdict, path, before, after, diff)
        with self._lock:
            self._pending[token] = pe

        return {
            "pending": True,
            "token": token,
            "summary": _summary(kind, path, before, after),
            "tier": verdict.tier.value,
            "auditor": verdict.decision.value,
            "reason": _reason(verdict),
            "diff": diff[:_MAX_DIFF_CHARS],
            "diff_truncated": len(diff) > _MAX_DIFF_CHARS,
            "requires_phrase": True,
            "note": "NOTHING has changed yet. This is a destructive edit: the user must "
                    f"type '{CONFIRM_PHRASE}' in the confirmation UI before it is applied. "
                    "Show them the diff and tell them it is waiting on that confirmation.",
        }

    # ------------------------------------------------------------------
    # Host surface
    # ------------------------------------------------------------------
    def pending(self) -> list[Dict[str, Any]]:
        with self._lock:
            return [p.public() for p in self._pending.values() if p.state == "pending"]

    def has(self, token: str) -> bool:
        with self._lock:
            return _norm_token(token) in self._pending

    def resolve(self, token: str, approved: bool, confirm_phrase: str = "") -> Dict[str, Any]:
        """Apply or reject a pending edit. This is the only writer in the path.

        A DESTRUCTIVE edit additionally requires the typed phrase. A wrong or
        missing phrase does NOT consume the token — the user can still settle
        it, which matters when a host renders the wrong button by mistake.
        """
        token = _norm_token(token)
        with self._lock:
            pe = self._pending.get(token)
            if pe is None:
                return {"error": f"unknown edit token {token!r}"}
            if pe.state != "pending":
                return {"error": f"edit {token} is already {pe.state}", "token": token}

        if not approved:
            with self._lock:
                pe.state = "rejected"
            self._record(pe, approved=False, outcome={"skipped": True})
            return {"token": token, "approved": False, "outcome": {"skipped": True}}

        if not _phrase_ok(confirm_phrase):
            self._record(pe, approved=False, outcome={"error": "confirm phrase not given"})
            return {
                "error": f"this edit is destructive — type '{CONFIRM_PHRASE}' to confirm it",
                "token": token,
                "requires_phrase": True,
            }

        # Stale check: never clobber a write that landed after the propose.
        try:
            current = _read_text(pe.path, must_exist=(pe.kind != "create"))
        except Exception as exc:
            with self._lock:
                pe.state = "stale"
            self._record(pe, approved=False, outcome={"error": str(exc)})
            return {"error": f"cannot re-read {pe.path}: {exc}", "token": token}
        if current != pe.before:
            with self._lock:
                pe.state = "stale"
            self._record(pe, approved=False, outcome={"error": "file changed since propose"})
            return {
                "error": f"{pe.path.name} changed since this edit was proposed; "
                         "propose it again against the current contents",
                "token": token,
            }

        try:
            _atomic_write(pe.path, pe.after)
        except Exception as exc:
            with self._lock:
                pe.state = "stale"  # unknown on-disk state; do not retry blindly
            self._record(pe, approved=False, outcome={"error": f"{type(exc).__name__}: {exc}"})
            return {"error": f"write failed: {type(exc).__name__}: {exc}", "token": token}

        with self._lock:
            pe.state = "applied"
            self._applied[token] = pe
            self._pending.pop(token, None)
        outcome = {
            "applied": True,
            "path": str(pe.path),
            "kind": pe.kind,
            "added": _count(pe.diff, "+"),
            "removed": _count(pe.diff, "-"),
        }
        self._record(pe, approved=True, outcome=outcome)
        return {"token": token, "approved": True, "outcome": outcome}

    def undo(self, token: str) -> Dict[str, Any]:
        """Restore the pre-edit contents of an edit applied in this session.

        Not a durable backup — git is the real undo for tracked files — but it
        makes an accidental confirmation recoverable without leaving JARVIS.
        """
        token = _norm_token(token)
        with self._lock:
            pe = self._applied.get(token)
            if pe is None:
                return {"error": f"no applied edit {token!r} to undo"}
        try:
            if pe.kind == "create":
                pe.path.unlink(missing_ok=True)
            else:
                _atomic_write(pe.path, pe.before)
        except Exception as exc:
            return {"error": f"undo failed: {type(exc).__name__}: {exc}"}
        with self._lock:
            self._applied.pop(token, None)
        return {"undone": True, "path": str(pe.path)}

    # ------------------------------------------------------------------
    # Preparation: validate, read, compute the new text. No writes.
    # ------------------------------------------------------------------
    def _prepare(self, params: Dict[str, Any]) -> Tuple[Optional[tuple], Optional[str]]:
        kind = params.get("action")
        if kind not in _VALID_ACTIONS:
            return None, f"action must be one of {', '.join(_VALID_ACTIONS)}"
        raw_path = str(params.get("path") or "").strip()
        if not raw_path:
            return None, "path is required"
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            return None, f"{raw_path!r} is not an absolute path; give the full path"
        if is_sensitive(str(path)):
            return None, (
                f"{path.name} looks like a credential or secret file; JARVIS does not "
                "edit those — change secrets by hand, outside the assistant"
            )
        bad = _REFUSED_PARTS.intersection(p.casefold() for p in path.parts)
        if bad:
            return None, f"refusing to edit inside {', '.join(sorted(bad))}"

        if kind == "create":
            if path.exists():
                return None, f"{path} already exists; use action='replace' or 'rewrite'"
            after = str(params.get("content") or "")
            if not after.strip():
                return None, "create needs non-empty 'content'"
            err = _size_check(after)
            return (kind, path, "", after), err

        try:
            before = _read_text(path, must_exist=True)
        except Exception as exc:
            return None, f"cannot read {path}: {exc}"

        if kind == "rewrite":
            after = str(params.get("content") or "")
            err = _size_check(after)
            if err:
                return None, err
            if after == before:
                return None, "the new content is identical to the file; nothing to change"
            return (kind, path, before, after), None

        # replace: old_string must occur exactly once.
        old = str(params.get("old_string") or "")
        new = str(params.get("new_string") or "")
        if not old:
            return None, "replace needs 'old_string'"
        if old == new:
            return None, "old_string and new_string are identical; nothing to change"
        occurrences = before.count(old)
        if occurrences == 0:
            return None, "old_string does not appear in the file (check whitespace and line endings)"
        if occurrences > 1:
            return None, (f"old_string appears {occurrences} times; include more surrounding "
                          "context so the edit targets exactly one place")
        after = before.replace(old, new, 1)
        err = _size_check(after)
        if err:
            return None, err
        return (kind, path, before, after), None

    # ------------------------------------------------------------------
    def _record(self, pe: PendingEdit, approved: bool, outcome: Dict[str, Any]) -> None:
        if self._audit is None:
            return
        try:
            import time as _time

            from openjarvis.security.types import SecurityEvent, SecurityEventType

            event = SecurityEvent(
                event_type=(SecurityEventType.TOOL_EXECUTED if approved
                            else SecurityEventType.TOOL_BLOCKED),
                timestamp=_time.time(),
                content_preview=(
                    f"file edit tool={EDIT_TOOL} kind={pe.kind} tier={pe.verdict.tier.value} "
                    f"approved={approved} path={pe.path} :: {outcome.get('error', '')}"
                ),
                action_taken=("approved" if approved else "denied"),
            )
            self._audit.log(event)
        except Exception:
            # Auditing must never block (or reverse) a write decision.
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


def _phrase_ok(phrase: str) -> bool:
    """All three words, in order, in any punctuation/spacing/case."""
    words = re.sub(r"[^a-z ]", " ", str(phrase or "").lower()).split()
    return tuple(words) == _PHRASE_TOKENS


def _size_check(text: str) -> Optional[str]:
    if len(text.encode("utf-8")) > _MAX_EDIT_BYTES:
        return f"the resulting file would exceed {_MAX_EDIT_BYTES // 1000} KB; edit it by hand"
    return None


def _read_text(path: Path, *, must_exist: bool) -> str:
    if not path.exists():
        if must_exist:
            raise FileNotFoundError(f"no such file: {path}")
        return ""
    if not path.is_file():
        raise IsADirectoryError(f"not a file: {path}")
    data = path.read_bytes()
    if len(data) > _MAX_EDIT_BYTES:
        raise ValueError(f"{path.name} is larger than {_MAX_EDIT_BYTES // 1000} KB")
    if b"\x00" in data:
        raise ValueError(f"{path.name} looks like a binary file")
    return data.decode("utf-8")


def _atomic_write(path: Path, text: str) -> None:
    """Write beside the target, copy its mode, then replace — no torn files.

    mkstemp creates 0600, so the mode is copied across or an edited script
    would silently lose its executable bit.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".jarvisedit")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _unified_diff(before: str, after: str, label: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{label}",
            tofile=f"b/{label}",
            n=3,
        )
    )


def _count(diff: str, sign: str) -> int:
    return sum(1 for line in diff.splitlines()
               if line.startswith(sign) and not line.startswith(sign * 3))


def _summary(kind: str, path: Path, before: str, after: str) -> str:
    added = removed = 0
    for line in _unified_diff(before, after, str(path)).splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    verb = {"replace": "edit", "rewrite": "rewrite", "create": "create"}[kind]
    return f"{verb} {path} (+{added} -{removed})"


def _reason(verdict: Verdict) -> str:
    if verdict.tier is Tier.DESTRUCTIVE:
        return "destructive: file edits require your typed confirmation"
    return verdict.reason


__all__ = [
    "SecuredFileEdits", "PendingEdit", "EDIT_TOOL", "CONFIRM_PHRASE",
    "EDIT_CONFIRM_RE", "EDIT_CANCEL_RE", "EDIT_BARE_YES_RE",
    "EDIT_PHRASE_MISPLACED_RE", "WRITE_CONFIRM_RE", "_VALID_ACTIONS",
]
