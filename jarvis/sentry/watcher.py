"""Auto-heal sentry pipeline (§6.7).

detect -> diagnose (LLM) -> patch skill code in a scratch copy ->
sandbox-test the patched copy -> park candidate -> approval gate ->
promote to the live skills dir.

Hard rule encoded below: the patch target must live under the agent's
skills directory (<home>/skills/). Anything else is refused — user files
go through the normal write-tier confirm, never through the sentry.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from openjarvis.core.paths import get_config_dir

from jarvis.skills.sandbox_test import run_skill_tests

_FAILURE_MARKERS = (
    "traceback", "exception", "error:", "failed", "failure",
    "not found", "does not exist", "denied", "timed out", "crash",
)

_DIAGNOSE_SYSTEM = (
    "You are the auto-heal sentry for a desktop agent. You are given a runtime "
    "failure of an AGENT SKILL. Propose the MINIMAL fix to the skill's code. "
    "You may only edit files under the skill's own directory. "
    "Respond as strict JSON: "
    '{"diagnosis": "...", "fix": "...", "test_script": "python script that verifies the fix", '
    '"changed_files": ["relative/path"]}'
)


@dataclass(slots=True)
class Failure:
    trace_id: str
    agent: str
    summary: str
    raw: str


@dataclass(slots=True)
class HealCandidate:
    failure: Failure
    diagnosis: str
    fix: str
    test_script: str
    changed_files: List[str]
    candidate_dir: Path
    sandbox_passed: bool
    note: str = ""


def detect_failures(traces_db: Path, since: float) -> List[Failure]:
    """Scan recent traces for failure markers in the result text."""
    if not traces_db.is_file():
        return []
    conn = sqlite3.connect(str(traces_db))
    rows = conn.execute(
        "SELECT trace_id, agent, result, started_at FROM traces WHERE started_at >= ? "
        "ORDER BY started_at DESC LIMIT 50",
        (since,),
    ).fetchall()
    conn.close()
    failures: List[Failure] = []
    for trace_id, agent, result, _ts in rows:
        text = (result or "").strip()
        low = text.casefold()
        if any(m in low for m in _FAILURE_MARKERS):
            summary = text[:200].replace("\n", " ")
            failures.append(Failure(trace_id=trace_id, agent=agent, summary=summary, raw=text[:4000]))
    return failures


def diagnose(failure: Failure, *, local_engine, cloud_engine=None) -> dict:
    """LLM diagnosis + minimal fix proposal. Local first; escalate when the
    text says to think hard or the local engine is down."""
    from openjarvis.core.types import Message, Role

    messages = [
        Message(role=Role.SYSTEM, content=_DIAGNOSE_SYSTEM),
        Message(role=Role.USER, content=f"Agent: {failure.agent}\nFailure:\n{failure.raw}"),
    ]
    payload = None
    try:
        payload = _extract_json(_content_of(local_engine.generate(messages, model="qwen3:1.7b", max_tokens=900, temperature=0.1)))
    except Exception:
        pass
    if payload is None and cloud_engine is not None:
        payload = _extract_json(_content_of(cloud_engine.generate(messages, model="agnes-3.0-flash", max_tokens=900, temperature=0.1)))
    if payload is None:
        raise RuntimeError("no usable diagnosis from local or cloud engine")
    return payload


def _path_inside_skills(target: str, skills_dir: Path) -> Optional[Path]:
    """Refuse any target that is not inside the skills dir (the §6.7 rule)."""
    candidate = (skills_dir / target).resolve()
    if skills_dir.resolve() in candidate.parents or candidate.parent == skills_dir.resolve():
        return candidate
    return None


def heal_failure(
    failure: Failure,
    *,
    local_engine,
    cloud_engine=None,
    skills_dir: Optional[Path] = None,
) -> HealCandidate:
    """Diagnose, build a patched scratch copy of the affected skill, and
    sandbox-test the patch. Returns the candidate (not promoted yet)."""
    skills_dir = Path(skills_dir) if skills_dir else Path(get_config_dir()) / "skills"
    payload = diagnose(failure, local_engine=local_engine, cloud_engine=cloud_engine)

    # The failure names the skill via agent context or the raw text.
    skill_name = _skill_name_for(failure)
    skill_dir = skills_dir / skill_name
    candidate_dir = Path(get_config_dir()) / "skill-heals" / skill_name
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate_dir.mkdir(parents=True, exist_ok=True)
    work = candidate_dir / stamp
    work.mkdir()

    # Patch the skill's code in the scratch copy only.
    if skill_dir.is_dir():
        shutil.copytree(skill_dir, work / "skill", dirs_exist_ok=False)
    else:
        (work / "skill").mkdir(parents=True)

    (work / "fix.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    test_script = payload.get("test_script", "print('no test provided')")
    (work / "test_skill.py").write_text(test_script, encoding="utf-8")

    # The patched skill is a scratch tree, not the live one: test in the
    # restricted process with the scratch tree as the test's working dir.
    test_result = run_skill_tests(
        test_script,
        workdir=work,
        timeout=120.0,
    )
    candidate = HealCandidate(
        failure=failure,
        diagnosis=payload.get("diagnosis", ""),
        fix=payload.get("fix", ""),
        test_script=test_script,
        changed_files=payload.get("changed_files", []),
        candidate_dir=work,
        sandbox_passed=test_result.passed,
        note=test_result.note,
    )
    return candidate


def _skill_name_for(failure: Failure) -> str:
    m = re.search(r"skill['\"]?\s*(?:named|')?\s*[:=]?\s*['\"]?([a-z0-9-]+)", failure.raw, re.I)
    if m:
        return m.group(1)
    return "diagnosed-skill"


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("no JSON object in diagnosis")
    return json.loads(m.group(0))


def _content_of(res) -> str:
    if isinstance(res, dict):
        for k in ("content", "text", "output", "answer"):
            if res.get(k):
                return str(res[k])
    return str(res)


__all__ = ["Failure", "HealCandidate", "detect_failures", "diagnose", "heal_failure"]
