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
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from openjarvis.core.paths import get_config_dir
from openjarvis.tools.code_interpreter import UnsafeCodeError, _validate_ast

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
    '{"diagnosis": "...", "fix": "...unified diff...", "test_script": "python script that verifies the fix", '
    '"changed_files": {"relative/path": "full replacement text"}}'
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


def _path_inside_skill(target: str, skill_root: Path) -> Optional[Path]:
    """Resolve a relative patch target inside one scratch skill directory."""
    if Path(target).is_absolute():
        return None
    candidate = (skill_root / target).resolve()
    root = skill_root.resolve()
    if root in candidate.parents or candidate.parent == root:
        return candidate
    return None


def _patch_suffix(target: str) -> str:
    """Strip unified-diff a/ or b/ prefixes from a patch target."""
    if target.startswith(("a/", "b/")):
        return target[2:]
    return target


def _apply_unified_diff(patch: str, skill_root: Path) -> None:
    """Apply a minimal unified diff to the scratch skill copy."""
    lines = patch.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0
    while index < len(lines):
        if not lines[index].startswith("--- "):
            index += 1
            continue
        old_name = _patch_suffix(lines[index][4:].strip())
        if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
            raise ValueError("unified diff is missing its +++ line")
        new_name = _patch_suffix(lines[index + 1][4:].strip())
        if old_name != new_name:
            raise ValueError("unified diff renames are not supported")
        target = _path_inside_skill(old_name, skill_root)
        if target is None:
            raise ValueError(f"patch target escapes the scratch skill: {old_name}")
        index += 2
        hunks = []
        while index < len(lines) and lines[index].startswith("@@"):
            header = lines[index]
            match = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", header)
            if not match:
                raise ValueError(f"malformed unified diff hunk: {header}")
            old_line = int(match.group(1))
            index += 1
            body: List[str] = []
            while index < len(lines) and not lines[index].startswith(("--- ", "@@")):
                line = lines[index]
                if line.startswith("\\"):
                    index += 1
                    continue
                if line[:1] not in (" ", "-", "+"):
                    break
                body.append(line)
                index += 1
            hunks.append((old_line, body))
        if not hunks:
            raise ValueError("unified diff has no hunks")
        original = target.read_text(encoding="utf-8").splitlines(keepends=True)
        offset = 0
        for old_line, body in hunks:
            start = max(0, old_line - 1 + offset)
            cursor = start
            rebuilt: List[str] = []
            for line in body:
                if line.startswith(" "):
                    if cursor >= len(original) or original[cursor].rstrip("\r\n") != line[1:]:
                        raise ValueError("unified diff context does not match the scratch skill")
                    rebuilt.append(original[cursor])
                    cursor += 1
                elif line.startswith("-"):
                    if cursor >= len(original) or original[cursor].rstrip("\r\n") != line[1:]:
                        raise ValueError("unified diff removal does not match the scratch skill")
                    cursor += 1
                else:
                    rebuilt.append(line[1:] + "\n")
            removed_or_context = cursor - start
            original[start:cursor] = rebuilt
            offset += len(rebuilt) - removed_or_context
        target.write_text("".join(original), encoding="utf-8")


def _apply_changed_files(changed_files, skill_root: Path) -> None:
    """Apply full-file replacements keyed by relative path."""
    if not isinstance(changed_files, dict):
        return
    for target_name, content in changed_files.items():
        target = _path_inside_skill(target_name, skill_root)
        if target is None:
            raise ValueError(f"changed file escapes the scratch skill: {target_name}")
        if not isinstance(content, str):
            raise ValueError(f"changed file content must be text: {target_name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _candidate_workdir(skill_name: str, *, candidate_root: Path) -> Path:
    """Create a unique candidate work directory for one heal attempt."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    work = candidate_root / skill_name / stamp
    work.mkdir(parents=True)
    return work


def heal_failure(
    failure: Failure,
    *,
    local_engine,
    cloud_engine=None,
    skills_dir: Optional[Path] = None,
    candidate_root: Optional[Path] = None,
) -> HealCandidate:
    """Diagnose, build a patched scratch copy of the affected skill, and
    sandbox-test the patch. Returns the candidate (not promoted yet)."""
    skills_dir = Path(skills_dir) if skills_dir else Path(get_config_dir()) / "skills"
    payload = diagnose(failure, local_engine=local_engine, cloud_engine=cloud_engine)

    # The failure names the skill via agent context or the raw text.
    skill_name = _skill_name_for(failure)
    skill_dir = skills_dir / skill_name
    root = Path(candidate_root) if candidate_root else Path(get_config_dir()) / "skill-heals"
    work = _candidate_workdir(skill_name, candidate_root=root)

    # Patch the skill's code in the scratch copy only.
    if skill_dir.is_dir():
        shutil.copytree(skill_dir, work / "skill", dirs_exist_ok=False)
    else:
        (work / "skill").mkdir(parents=True)

    (work / "fix.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    changed_files = payload.get("changed_files", [])
    fix = payload.get("fix", "")
    scratch_skill = work / "skill"
    patch_note = ""
    try:
        if isinstance(changed_files, dict):
            _apply_changed_files(changed_files, scratch_skill)
        elif fix:
            _apply_unified_diff(fix, scratch_skill)
    except ValueError as exc:
        patch_note = f"patch rejected: {exc}"
    test_script = payload.get("test_script", "print('no test provided')")
    test_script = str(test_script)
    test_passed = False
    test_note = ""
    try:
        # The script is model output driven by a failure trace, so it gets the
        # same structural gate as sandbox_execute before touching disk.
        _validate_ast(test_script)
    except (UnsafeCodeError, SyntaxError) as exc:
        test_note = f"test script rejected: {exc}"
    else:
        (work / "test_skill.py").write_text(test_script, encoding="utf-8")

        # The patched skill is a scratch tree, not the live one: test in the
        # restricted process with the scratch tree as the test's working dir.
        # Docker is required here: the process fallback runs as this user.
        test_result = run_skill_tests(
            test_script,
            workdir=work,
            timeout=120.0,
            prefer="docker",
        )
        test_passed = test_result.passed if not patch_note else False
        test_note = test_result.note
    candidate = HealCandidate(
        failure=failure,
        diagnosis=payload.get("diagnosis", ""),
        fix=fix,
        test_script=test_script,
        changed_files=changed_files,
        candidate_dir=work,
        sandbox_passed=test_passed,
        note=f"{patch_note}; {test_note}" if patch_note else test_note,
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
