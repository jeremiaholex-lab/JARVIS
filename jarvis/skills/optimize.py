"""Trace-driven skill optimization (design §6.6 step 4).

The design says OpenJarvis's DSPy-based optimizer can improve a skill from
actual usage, behind the same approval gate. DSPy is not installed on this
machine (`pip show dspy` → absent), so this module implements the *behaviour*
the design asks for — read the traces, find where the skill under-performed,
propose a better SKILL.md, test it in the sandbox, and park it for approval —
with a plain extract→propose→verify loop instead of a DSPy teleprompter.

Swap-in point: `propose_improvement()` is the only function that would change
if DSPy were installed; everything around it (evidence, sandbox, gate) stays.

Hard rules, same as the sentry:
  * the live skill is never mutated here;
  * a proposal without evidence is refused (no optimizing on nothing);
  * tests must pass AND the owner must approve before promotion.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from openjarvis.core.paths import get_config_dir

from jarvis.skills.approve import SkillGovernance
from jarvis.skills.propose import Proposal, validate_proposal
from jarvis.skills.sandbox_test import run_skill_tests

_logger = logging.getLogger(__name__)

_FAILURE_MARKERS = (
    "traceback", "exception", "error:", "failed", "not found",
    "does not exist", "denied", "timed out", "no such file", "unable to",
)
_LOW_FEEDBACK = 0.34


@dataclass(slots=True)
class Evidence:
    trace_id: str
    query: str
    result: str
    outcome: str
    feedback: Optional[float]
    latency_s: float
    started_at: float

    @property
    def bad(self) -> bool:
        if self.outcome and self.outcome.casefold() not in ("success", "ok", "completed", "passed"):
            return True
        if self.feedback is not None and self.feedback < _LOW_FEEDBACK:
            return True
        lowered = (self.result or "").casefold()
        return any(marker in lowered for marker in _FAILURE_MARKERS)


@dataclass(slots=True)
class Analysis:
    skill: str
    total: int
    bad: int
    patterns: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)

    @property
    def has_signal(self) -> bool:
        return self.bad > 0 and bool(self.patterns)


def _traces_db(home: Path) -> Path:
    return Path(home) / "traces.db"


def collect_evidence(skill: str, *, traces_db: Optional[Path] = None,
                     since: float = 0.0, limit: int = 200) -> list[Evidence]:
    """Read usage for one skill out of the trace store.

    Traces name the agent, not always the skill, so the skill name is matched
    against the query/result text as well as metadata.
    """
    db = Path(traces_db) if traces_db else _traces_db(Path(get_config_dir()))
    if not db.is_file():
        return []
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT trace_id, query, agent, result, outcome, feedback, "
            "total_latency_seconds, started_at, metadata FROM traces "
            "WHERE started_at >= ? ORDER BY started_at DESC LIMIT ?",
            (since, limit),
        ).fetchall()
    finally:
        conn.close()

    needle = skill.casefold()
    evidence: list[Evidence] = []
    for row in rows:
        haystack = " ".join(filter(None, (
            row["query"], row["agent"], row["result"], row["metadata"],
        ))).casefold()
        if needle not in haystack:
            continue
        evidence.append(Evidence(
            trace_id=row["trace_id"], query=row["query"] or "",
            result=(row["result"] or "")[:2000], outcome=row["outcome"] or "",
            feedback=row["feedback"], latency_s=row["total_latency_seconds"] or 0.0,
            started_at=row["started_at"],
        ))
    return evidence


def analyze(skill: str, evidence: Sequence[Evidence]) -> Analysis:
    """Deterministic pattern mining — no model needed to know it failed."""
    bad = [e for e in evidence if e.bad]
    patterns: list[str] = []
    counts: dict[str, int] = {}
    for item in bad:
        lowered = (item.result or "").casefold()
        for marker in _FAILURE_MARKERS:
            if marker in lowered:
                counts[marker] = counts.get(marker, 0) + 1
    for marker, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        patterns.append(f"{marker.strip()} appeared in {n} run(s)")
    low_rated = [e for e in bad if e.feedback is not None and e.feedback < _LOW_FEEDBACK]
    if low_rated:
        patterns.append(f"{len(low_rated)} run(s) got explicit low user feedback")
    slow = [e for e in evidence if e.latency_s > 30]
    if slow and not bad:
        patterns.append(f"{len(slow)} run(s) took over 30s")
    return Analysis(
        skill=skill, total=len(evidence), bad=len(bad), patterns=patterns,
        examples=[(e.query or e.result)[:160] for e in bad[:3]],
    )


_SYSTEM = (
    "You improve agent skills. You are given the CURRENT SKILL.md and evidence "
    "about where it under-performed in real usage. Rewrite the skill so those "
    "failures cannot recur: sharper instructions, explicit preconditions, the "
    "exact command shapes to run, and what to do when the expected input is "
    "missing. Keep the YAML frontmatter (name, description) valid and keep the "
    "name unchanged. Change only what the evidence justifies. A skill is "
    "MARKDOWN INSTRUCTIONS ONLY - it ships no importable module, so the test "
    "script must be self-contained: standard library only, no 'import skill'. "
    "Respond as JSON: "
    '{"skill_md": "...", "rationale": "what changed and which evidence it came from", '
    '"test_script": "python that proves the fixed behaviour"}. '
    "The test script receives its scratch directory as argv[1], writes any "
    "sample files there, and must assert rather than merely print."
)


def propose_improvement(
    skill_name: str,
    current_skill_md: str,
    analysis: Analysis,
    *,
    local_engine=None,
    cloud_engine=None,
) -> dict:
    """Ask a model for a better SKILL.md + a test that proves it.

    Local first; the cloud engine is used only if the local one produces
    nothing usable. Returns {} on failure so the caller can refuse cleanly.
    """
    from openjarvis.core.types import Message, Role

    brief = (
        f"SKILL.md for '{skill_name}':\n{current_skill_md}\n\n"
        f"Usage evidence: {analysis.total} run(s), {analysis.bad} bad.\n"
        + "\n".join(f"- {p}" for p in analysis.patterns)
        + "\n\nFailing examples:\n"
        + "\n".join(f"- {e}" for e in analysis.examples)
    )
    messages = [
        Message(role=Role.SYSTEM, content=_SYSTEM),
        Message(role=Role.USER, content=brief),
    ]
    for engine, model in ((local_engine, "qwen3:1.7b"), (cloud_engine, "agnes-3.0-flash")):
        if engine is None:
            continue
        try:
            res = engine.generate(messages, model=model, max_tokens=1600, temperature=0.2)
            payload = _json_object(_content_of(res))
            if payload.get("skill_md") and payload.get("test_script"):
                payload["_engine"] = model
                return payload
        except Exception as exc:
            _logger.warning("optimization proposal failed on %s (%s)", model, exc)
    return {}


def optimize_skill(
    skill: str,
    *,
    skills_dir: Optional[Path] = None,
    home: Optional[Path] = None,
    local_engine=None,
    cloud_engine=None,
    auto_approve: bool = False,
    dry_run: bool = False,
    input_fn=None,
    print_fn=print,
) -> int:
    """Improve one skill from its own usage. Never promotes without approval."""
    home = Path(home) if home else Path(get_config_dir())
    skills_dir = Path(skills_dir) if skills_dir else home / "skills"
    skill_md = skills_dir / skill / "SKILL.md"
    if not skill_md.is_file():
        print_fn(f"[optimise] no live skill at {skill_md} — nothing to improve")
        return 2

    evidence = collect_evidence(skill, traces_db=_traces_db(home))
    analysis = analyze(skill, evidence)
    print_fn(f"[optimise] {skill}: {analysis.total} trace(s) reviewed, {analysis.bad} bad")
    for pattern in analysis.patterns:
        print_fn(f"           - {pattern}")
    if not analysis.has_signal:
        print_fn("[optimise] no failure signal to learn from — skill left as is "
                 "(optimizing without evidence is how a skill gets worse)")
        return 0

    if dry_run:
        print_fn("[optimise] --dry-run: proposal not generated, nothing changed")
        return 0

    payload = propose_improvement(
        skill, skill_md.read_text(encoding="utf-8"), analysis,
        local_engine=local_engine, cloud_engine=cloud_engine,
    )
    if not payload:
        print_fn("[optimise] no usable proposal from any engine — skill unchanged")
        return 1

    proposed_md = payload["skill_md"]
    try:
        manifest = validate_proposal(proposed_md)
    except Exception as exc:
        print_fn(f"[optimise] rejected: proposed SKILL.md does not parse: {exc}")
        return 1
    if manifest.name != skill:
        print_fn(f"[optimise] rejected: proposal renamed the skill to {manifest.name!r}")
        return 1

    print_fn(f"[optimise] rationale: {str(payload.get('rationale', ''))[:300]}")
    from jarvis.skills.propose import check_test_script
    from jarvis.skills.propose import ProposalError

    try:
        check_test_script(payload["test_script"])
    except ProposalError as exc:
        print_fn(f"[optimise] rejected proposal: {exc}")
        return 1

    test = run_skill_tests(payload["test_script"], timeout=120.0)
    print_fn(f"[optimise] sandbox: passed={test.passed} isolation={test.isolation} "
             f"rc={test.returncode} {test.note}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    park_dir = home / "skill-optimisations" / skill / stamp
    park_dir.mkdir(parents=True, exist_ok=True)
    (park_dir / "SKILL.md").write_text(proposed_md, encoding="utf-8")
    (park_dir / "test_skill.py").write_text(payload["test_script"], encoding="utf-8")
    (park_dir / "analysis.json").write_text(json.dumps({
        "skill": skill, "engine": payload.get("_engine", ""), "patterns": analysis.patterns,
        "traces_reviewed": analysis.total, "bad_traces": analysis.bad,
        "sandbox_passed": test.passed, "isolation": test.isolation,
    }, indent=2), encoding="utf-8")
    print_fn(f"[optimise] candidate parked at {park_dir}")

    if not test.passed:
        print_fn("[optimise] tests did not pass — nothing offered for approval")
        return 1

    proposal = Proposal(
        name=skill, manifest=manifest, skill_md=proposed_md,
        test_script=payload["test_script"],
        summary=f"optimised from {analysis.bad} failing run(s): "
                f"{'; '.join(analysis.patterns[:3])}",
        source_gap=f"trace evidence for {skill}",
    )
    approved = SkillGovernance(home=home).request_approval(
        proposal, test, input_fn=(lambda _p: "approve" if auto_approve else (input_fn or input)(_p)),
        print_fn=print_fn,
    )
    if approved:
        print_fn(f"[optimise] approved — {skill} replaced; next session loads the improved skill")
        return 0
    print_fn("[optimise] not approved — live skill untouched")
    return 1


def _json_object(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _content_of(res) -> str:
    if isinstance(res, dict):
        for key in ("content", "text", "output", "answer"):
            if res.get(key):
                return str(res[key])
    return str(res)


__all__ = [
    "Evidence", "Analysis", "collect_evidence", "analyze",
    "propose_improvement", "optimize_skill",
]
