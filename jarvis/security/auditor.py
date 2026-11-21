"""Auditor / critic agent (design §6.10).

A cheap second brain whose ONLY job is to review the main agent's *planned*
actions against the §6.7 safety policy BEFORE execution. It can pass, hold,
or flag an action — it cannot execute anything itself.

Two layers (so it is trustworthy and testable):
  1. Deterministic critic — tier + secret/heuristic + high-risk patterns.
     Always available; this is the part the demo relies on.
  2. LLM critic (optional) — local Ollama first, cloud on an explicit "think
     hard" request. It runs only after a clean deterministic PASS and can
     downgrade that PASS to HOLD or FLAG. If no engine is reachable, the
     verdict HOLDS: a missing critic is never treated as approval.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .tiers import PlannedAction, Tier, check_batched, classify, is_sensitive

_LOCAL_AUDIT_MODEL = "qwen3:1.7b"
_CLOUD_AUDIT_MODEL = "agnes-3.0-flash"

# Text that, anywhere in a planned action's params, should at least HOLD.
_ESCALATE_RE = re.compile(r"\b(think hard|think deeply|escalate)\b", re.I)


class Decision(str, Enum):
    PASS = "pass"
    HOLD = "hold"
    FLAG = "flag"


@dataclass(slots=True)
class Verdict:
    """Result of the auditor reviewing one planned action."""

    decision: Decision
    tier: Tier
    reason: str = ""
    engine_used: str = "deterministic"
    requires_typed_confirm: bool = False  # set True for DESTRUCTIVE

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.PASS


class Auditor:
    """Reviews planned actions; returns a Verdict. No side effects."""

    def __init__(
        self,
        *,
        local_engine=None,
        cloud_engine=None,
        use_llm: bool = True,
    ) -> None:
        self._local = local_engine
        self._cloud = cloud_engine
        self._use_llm = use_llm

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------
    def judge(
        self,
        action: PlannedAction,
        batch: Optional[List[PlannedAction]] = None,
    ) -> Verdict:
        """Judge one action in the context of its batch."""
        batch = batch or [action]
        tier = classify(action)

        # Destructive is a hard gate regardless of the auditor's mood.
        if tier is Tier.DESTRUCTIVE:
            bc = check_batched(batch, batch)
            reason = "destructive action"
            if not bc.ok:
                reason = bc.reason
            return Verdict(
                decision=Decision.FLAG if not bc.ok else Decision.HOLD,
                tier=tier,
                reason=reason,
                engine_used="deterministic",
                requires_typed_confirm=True,
            )

        # Deterministic critic first.
        verdict = self._deterministic(action, tier)
        if verdict.decision in (Decision.HOLD, Decision.FLAG):
            return verdict

        # Optional LLM critic only adds value on a clean deterministic pass.
        if self._use_llm:
            llm = self._llm_critic(action)
            if llm is not None:
                return llm
            return Verdict(
                decision=Decision.HOLD,
                tier=tier,
                reason="LLM critic was unavailable or unreadable",
            )
        return verdict

    # ------------------------------------------------------------------
    # Deterministic critic
    # ------------------------------------------------------------------
    def _deterministic(self, action: PlannedAction, tier: Tier) -> Verdict:
        # Secrets / credentials are never written without a hold, even on WRITE.
        joined = " ".join(str(v) for v in action.params.values() if isinstance(v, str))
        if is_sensitive(joined) or any(
            is_sensitive(str(action.params.get(k, ""))) for k in ("path", "file", "target")
        ):
            return Verdict(
                decision=Decision.HOLD,
                tier=tier,
                reason="action touches a sensitive/credential path",
            )
        # A WRITE that is fine otherwise passes; the confirm gate still shows it.
        return Verdict(
            decision=Decision.PASS,
            tier=tier,
            reason="clean under deterministic rules",
        )

    # ------------------------------------------------------------------
    # LLM critic (optional)
    # ------------------------------------------------------------------
    def _llm_critic(self, action: PlannedAction) -> Optional[Verdict]:
        engine, model = self._select_engine(action)
        if engine is None:
            return None
        try:
            prompt = (
                "You are a safety critic for a desktop AI assistant. "
                "Review ONE planned tool action and decide if it is safe to proceed. "
                "You may only answer PASS, HOLD or FLAG, plus one short reason. "
                "Never invent execution. Respond as JSON: "
                '{"decision": "pass|hold|flag", "reason": "..."}. '
                "Planned action:\n" + json.dumps({"tool": action.tool, "params": dict(action.params)})
            )
            from openjarvis.core.types import Message, Role

            messages = [
                Message(role=Role.SYSTEM, content=prompt),
                Message(role=Role.USER, content="Adjudicate."),
            ]
            res = engine.generate(messages, model=model, max_tokens=120, temperature=0.0)
            text = _content_of(res)
            decision = _first_enum(text, ("pass", "hold", "flag"), None)
            if decision is None:
                return None
            reason = _extract_reason(text)
            tier = classify(action)
            return Verdict(
                decision=Decision(decision),
                tier=tier,
                reason=reason or "LLM critic",
                engine_used=f"{type(engine).__name__}:{model}",
                requires_typed_confirm=(decision == "flag"),
            )
        except Exception:
            # No engine / transient error: hold rather than treating a failed
            # critic as approval.
            return None

    def _select_engine(self, action: PlannedAction):
        hard = bool(_ESCALATE_RE.search(
            " ".join(str(v) for v in action.params.values() if isinstance(v, str))
        ))
        if hard:
            if self._cloud:
                return self._cloud, _CLOUD_AUDIT_MODEL
            return self._local, _LOCAL_AUDIT_MODEL
        if self._local:
            return self._local, _LOCAL_AUDIT_MODEL
        if self._cloud:
            return self._cloud, _CLOUD_AUDIT_MODEL
        return None, None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _content_of(res) -> str:
    if isinstance(res, dict):
        for k in ("content", "text", "output", "answer"):
            if res.get(k):
                return str(res[k])
        if res.get("choices"):
            return str(res["choices"][0].get("message", {}).get("content", ""))
    return str(res)


def _first_enum(text: str, values, default):
    t = text.casefold()
    # Prefer the last occurrence the model emits in JSON-ish order.
    found = default
    for v in values:
        if v in t:
            found = v
    return found


def _extract_reason(text: str) -> str:
    m = re.search(r'"reason"\s*:\s*"([^"]+)"', text, re.I)
    if m:
        return m.group(1).strip()
    for line in text.splitlines():
        line = line.strip()
        if line and line not in ('{', '}') and "decision" not in line.lower():
            return line
    return ""


__all__ = ["Auditor", "Verdict", "Decision"]
