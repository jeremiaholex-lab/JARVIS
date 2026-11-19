"""Secure confirm gate (design §6.7) — the user-facing half of Phase 4 Step 1.

The framework's ToolExecutor already routes every confirmed tool call through
a `confirm_callback(prompt: str) -> bool` (openjarvis/tools/_stubs.py:400-418,
prompt format: "Allow execution of tool '<name>' with args <params>?").
JARVIS supplies its own callback here and monkey-patches the CLI's one —
no framework edits.

Flow per prompt:
  1. Parse (tool, params) back out of the prompt string.
  2. Ask the Auditor (deterministic + optional LLM critic).
  3. Tier -> prompt style:
       READ        (structurally free; the framework wouldn't ask, but if it
                    does, one-tap confirm)
       WRITE       one-tap confirm; the tool keeps its undo record.
       DESTRUCTIVE explicit TYPED confirmation ("yes, do it"), never batched.
  4. Log the decision to the hash-chained AuditLogger.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Optional

import logging

from .auditor import Auditor, Decision, Verdict
from .tiers import PlannedAction, Tier, classify

# Module logger
_logger = logging.getLogger(__name__)

# Exact prompt shape emitted by ToolExecutor (tools/_stubs.py:412).
_PROMPT_RE = re.compile(
    r"Allow execution of tool '(?P<tool>[^']+)' with args (?P<args>.*)\?$",
    re.DOTALL,
)


def parse_prompt(prompt: str) -> PlannedAction:
    """Turn the framework's confirm prompt back into a PlannedAction."""
    m = _PROMPT_RE.search(prompt.strip())
    if not m:
        # Unknown shape — treat conservatively as an unparseable WRITE.
        return PlannedAction(tool="unknown", params={"prompt": prompt})
    args_raw = m.group("args").strip()
    try:
        params = json.loads(args_raw)
        if not isinstance(params, dict):
            params = {"value": params}
    except (json.JSONDecodeError, TypeError):
        params = {"raw": args_raw}
    return PlannedAction(tool=m.group("tool"), params=params)


class ConfirmGate:
    """Drop-in replacement for the CLI's _confirm_tool_action."""

    def __init__(
        self,
        auditor: Auditor,
        *,
        audit_logger=None,
        input_fn: Callable[[str], str] = input,
        print_fn=print,
    ) -> None:
        self.auditor = auditor
        self._audit = audit_logger
        self._input = input_fn
        self._print = print_fn

    # The framework calls this with a single prompt string.
    def __call__(self, prompt: str) -> bool:
        action = parse_prompt(prompt)
        verdict = self.auditor.judge(action)
        outcome = self._prompt_for(action, verdict)
        self._record(action, verdict, outcome)
        return outcome

    # ------------------------------------------------------------------
    def _prompt_for(self, action: PlannedAction, verdict: Verdict) -> bool:
        if verdict.decision is Decision.FLAG:
            self._print(f"  [auditor] FLAGGED — {verdict.reason} (tier: {verdict.tier})")
            return self._typed_confirm(action, verdict)

        tier = verdict.tier
        if tier is Tier.DESTRUCTIVE:
            self._print(f"  [auditor] destructive hard-gate: {verdict.reason}")
            return self._typed_confirm(action, verdict)

        if verdict.decision is Decision.HOLD:
            self._print(f"  [auditor] HOLD — {verdict.reason}")
            return self._one_tap(action, verdict, allow_over=False)

        # PASS: READ is free by policy; the framework wouldn't have asked,
        # but if it did, one-tap is enough and no gate is added.
        if tier is Tier.READ:
            return True
        return self._one_tap(action, verdict, allow_over=True)

    def _one_tap(self, action: PlannedAction, verdict: Verdict, *, allow_over: bool) -> bool:
        tool, params = action.tool, action.params
        preview = _preview(tool, params)
        if not allow_over:
            answer = self._input(f"  [jarvis] {verdict.reason}. Still allow {tool}? [y/N] ")
            ok = answer.strip().lower() == "y"
        else:
            answer = self._input(f"  [jarvis] Confirm write {preview}? [Y/n] ")
            ok = answer.strip().lower() in ("", "y")
        return ok

    def _typed_confirm(self, action: PlannedAction, verdict: Verdict) -> bool:
        """§6.7 hard gate: the user must type the magic phrase, and it is
        never batched — this callback only ever sees one action."""
        action_preview = _preview(action.tool, action.params)
        self._print(f"  [jarvis] {action_preview}")
        self._print("  [jarvis] DESTRUCTIVE action — type 'yes, do it' to confirm, anything else cancels.")
        answer = self._input("  [typed] ")
        return answer.strip() == "yes, do it"

    # ------------------------------------------------------------------
    def _record(self, action: PlannedAction, verdict: Verdict, approved: bool) -> None:
        if self._audit is None:
            return
        from openjarvis.security.types import SecurityEvent, SecurityEventType

        import time as _time

        held = verdict.decision in (Decision.HOLD, Decision.FLAG) and not approved
        event_type = (
            SecurityEventType.TOOL_BLOCKED if held
            else SecurityEventType.TOOL_EXECUTED
        )
        event = SecurityEvent(
            event_type=event_type,
            timestamp=_time.time(),
            content_preview=(
                f"auditor tool={action.tool} tier={verdict.tier.value} "
                f"verdict={verdict.decision.value} engine={verdict.engine_used} "
                f"approved={approved} :: {verdict.reason}"
            ),
            action_taken="approved" if approved else "denied",
        )
        try:
            self._audit.log(event)
            _logger.debug(
                "Audit record logged",
                extra={"extra_data": {
                    "tool": action.tool,
                    "tier": verdict.tier.value,
                    "decision": verdict.decision.value,
                    "approved": approved,
                }},
            )
        except Exception as exc:
            # auditing must never block the agent loop
            _logger.warning("Failed to write audit record: %s", exc)


def _preview(tool: str, params) -> str:
    parts = []
    for key in ("path", "file", "command", "target", "url"):
        if params.get(key):
            parts.append(f"{key}={params[key]!r}")
    if not parts:
        s = json.dumps(params, default=str)
        parts.append(s if len(s) <= 120 else s[:117] + "...")
    return f"{tool} " + " ".join(parts[:3])


__all__ = ["ConfirmGate", "parse_prompt"]
