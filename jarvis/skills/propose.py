"""Agent-proposed skill drafting (§6.6 step 1).

The agent notices a capability gap and proposes a new skill in
agentskills.io format (SKILL.md + frontmatter) together with a small
spec/test set. The LLM writes the draft; this module validates it
against the framework's SkillParser so a malformed proposal never
reaches the sandbox.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from openjarvis.skills.parser import SkillParseError, SkillParser
from openjarvis.skills.types import SkillManifest

_logger = logging.getLogger(__name__)


class ProposalError(RuntimeError):
    """Every engine produced something unusable."""

_SYSTEM = (
    "You author agent skills in the agentskills.io format. A skill is MARKDOWN "
    "INSTRUCTIONS ONLY — it ships no importable Python module, so a test script "
    "must never do `from skill import ...` or import the skill by name. "
    "Given a capability gap, produce: (1) SKILL.md text with YAML frontmatter "
    "(name: kebab-case, description: one line) describing the procedure the "
    "agent should follow, (2) a SELF-CONTAINED python test script that performs "
    "the described computation inline (writing any sample file it needs into "
    "the directory passed as argv[1], using only the standard library, with "
    "assert statements that fail loudly), (3) a one-line summary of what it "
    "touches. Respond as strict JSON: "
    '{"skill_md": "...", "test_script": "...", "summary": "what it does, what it can touch"}.'
)


@dataclass(slots=True)
class Proposal:
    name: str
    manifest: SkillManifest
    skill_md: str
    test_script: str
    summary: str
    source_gap: str


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\n(.*?)\n---\n?", text, re.DOTALL)
    if not m:
        raise ValueError("SKILL.md must begin with YAML frontmatter (---)")
    import yaml

    fm = yaml.safe_load(m.group(1)) or {}
    return fm, text[m.end():]


_SKILL_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+skill\s+import|import\s+skill\b|from\s+[a-z_]+\s+import.*#\s*skill)",
    re.I | re.MULTILINE,
)


def check_test_script(test_script: str) -> None:
    """Reject the failure mode seen live: a test importing a module the skill
    never provides. Cheaper and clearer than a container run saying ImportError.
    """
    if _SKILL_IMPORT_RE.search(test_script or ""):
        raise ProposalError(
            "test script imports a module the skill does not provide — skills "
            "are markdown instructions, not importable code. The test must "
            "implement the described behaviour inline."
        )


def validate_proposal(skill_md: str) -> SkillManifest:
    """Run the framework's strict parser on the proposed SKILL.md."""
    fm, body = _parse_frontmatter(skill_md)
    manifest = SkillParser().parse_frontmatter(fm, markdown_content=body)
    manifest.user_invocable = True  # JARVIS-promoted skills are user-invocable
    return manifest


def propose_skill(
    gap: str,
    *,
    local_engine,
    cloud_engine=None,
    use_cloud: bool = False,
) -> Proposal:
    """Ask the local model (escalating to cloud on failure or 'think hard')
    to draft a skill + its test set, then validate before anything runs.

    Validation is part of each attempt: the small local model sometimes emits
    a SKILL.md with broken frontmatter, and letting that raise past here used
    to crash `phase4.py skill` instead of trying the cloud draft. A malformed
    proposal must be rejected and escalated, not explode.
    """
    from openjarvis.core.types import Message, Role

    messages = [
        Message(role=Role.SYSTEM, content=_SYSTEM),
        Message(role=Role.USER, content=f"Capability gap: {gap}"),
    ]
    attempts = []
    if use_cloud and cloud_engine is not None:
        attempts.append((cloud_engine, "agnes-3.0-flash"))
    else:
        if local_engine is not None:
            attempts.append((local_engine, "qwen3:1.7b"))
        if cloud_engine is not None:
            attempts.append((cloud_engine, "agnes-3.0-flash"))

    problems: list[str] = []
    for engine, model in attempts:
        try:
            raw = _content_of(engine.generate(messages, model=model, max_tokens=1200, temperature=0.2))
        except Exception as exc:
            problems.append(f"{model}: engine failed ({exc.__class__.__name__})")
            continue
        try:
            payload = _extract_json(raw)
            skill_md, test_script = payload["skill_md"], payload["test_script"]
            check_test_script(test_script)
            manifest = validate_proposal(skill_md)
        except Exception as exc:
            problems.append(f"{model}: {exc.__class__.__name__}: {exc}")
            _logger.warning("discarding malformed skill proposal from %s: %s", model, exc)
            continue
        return Proposal(
            name=manifest.name,
            manifest=manifest,
            skill_md=skill_md,
            test_script=test_script,
            summary=payload.get("summary", ""),
            source_gap=gap,
        )

    raise ProposalError(
        "no engine produced a valid skill proposal\n  " + "\n  ".join(problems)
        + "\n(use --no-llm for the built-in demo proposal)"
    )


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("model did not return a JSON object")
    return json.loads(m.group(0))


def _content_of(res) -> str:
    if isinstance(res, dict):
        for k in ("content", "text", "output", "answer"):
            if res.get(k):
                return str(res[k])
    return str(res)


__all__ = ["Proposal", "ProposalError", "propose_skill", "validate_proposal",
           "check_test_script"]
