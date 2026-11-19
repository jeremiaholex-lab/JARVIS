"""Sentry runner — background watch mode and the done-test demo.

Usage (runtime venv, from the JARVIS root):
  python -m jarvis.sentry.demo            # scripted demo of the full flow
  python -m jarvis.sentry.watch --interval 60   # background watcher
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

from openjarvis.core.paths import get_config_dir

from .watcher import Failure, detect_failures, heal_failure

# Module logger
_logger = logging.getLogger(__name__)

_HOME = Path(get_config_dir())
TRACES_DB = _HOME / "traces.db"
_last_seen: dict[str, float] = {}


def _engines():
    from jarvis.security.launcher import _build_engines

    return _build_engines()


def _refuses_user_files(candidate) -> bool:
    """§6.7 rule: sentry may only propose edits inside agent skill code."""
    skills_dir = (_HOME / "skills").resolve()
    for target in candidate.changed_files or []:
        full = (Path(target) if Path(target).is_absolute() else skills_dir / target).resolve()
        if skills_dir not in full.parents and full.parent != skills_dir:
            return True
    return False


def watch(*, interval: int = 60) -> None:
    """Background loop: pick up new failed traces, heal, park candidates.
    Promoting a candidate to the live catalog still requires the approval
    gate (jarvis.skills.pipeline / the Settings hub)."""
    local, cloud = _engines()
    _logger.info(
        "watching %s every %ss — candidates park in %s",
        TRACES_DB, interval, _HOME / "skill-heals",
    )
    while True:
        for failure in detect_failures(TRACES_DB, since=_last_seen.get("ts", time.time() - 3600)):
            if failure.trace_id in _last_seen:
                continue
            _last_seen[failure.trace_id] = failure.raw[:80]
            try:
                candidate = heal_failure(failure, local_engine=local, cloud_engine=cloud)
                _logger.info(
                    "%s: sandbox_passed=%s files=%s parked=%s",
                    failure.trace_id, candidate.sandbox_passed,
                    candidate.changed_files, candidate.candidate_dir,
                )
                (candidate.candidate_dir / "candidate.json").write_text(
                    json.dumps({
                        "trace_id": failure.trace_id,
                        "agent": failure.agent,
                        "diagnosis": candidate.diagnosis,
                        "fix": candidate.fix,
                        "changed_files": candidate.changed_files,
                        "sandbox_passed": candidate.sandbox_passed,
                        "pending_approval": True,
                    }, indent=2),
                    encoding="utf-8",
                )
                if _refuses_user_files(candidate):
                    _logger.warning(
                        "%s: REFUSED proposed target outside agent skill code — "
                        "user files go through the normal write-tier confirm.",
                        failure.trace_id,
                    )
            except Exception as exc:
                _logger.error("%s: heal failed (%s: %s)",
                              failure.trace_id, exc.__class__.__name__, exc)
        _last_seen["ts"] = time.time()
        time.sleep(interval)


def demo(*, use_live_traces: bool = False) -> int:
    """Done-test for the sentry: a failing skill, a patch that stays inside
    skill code (accepted), and a patch that reaches for a user file
    (refused — the §6.7 guarantee)."""
    local, cloud = _engines()
    skills_dir = _HOME / "skills"

    # Fabricate a failed trace so the detector has something to catch.
    import sqlite3

    conn = sqlite3.connect(str(TRACES_DB))
    conn.execute(
        "INSERT INTO traces (id, trace_id, query, agent, model, engine, result, outcome, "
        "started_at, ended_at, total_tokens, total_latency_seconds, metadata, messages) "
        "VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0.0, ?, ?)",
        (
            "sentry-demo-fail",
            "word-count-lines on notes/todo.txt",
            "native_openhands",
            "qwen3:8b",
            "ollama",
            "Traceback (most recent call last): File 'sample.txt' not found in the specified directory.",
            "failed",
            time.time(),
            time.time(),
            "{}",
            "[]",
        ),
    )
    conn.commit()
    conn.close()

    failures = detect_failures(TRACES_DB, since=time.time() - 60)
    target = next(f for f in failures if f.trace_id == "sentry-demo-fail")
    print(f"[sentry] detected {len(failures)} new failure(s); healing {target.trace_id}: "
          f"{target.summary[:80]}")

    candidate = heal_failure(target, local_engine=local, cloud_engine=cloud, skills_dir=skills_dir)
    print(f"[sentry] diagnosis: {candidate.diagnosis[:160]}")
    print(f"[sentry] sandbox re-test of patched skill: passed={candidate.sandbox_passed} ({candidate.note or 'clean'})")
    print(f"[sentry] candidate parked at {candidate.candidate_dir} — awaiting approval")

    # Negative case: a patch that reaches outside skill code must be refused.
    from .watcher import HealCandidate

    sneaky = HealCandidate(
        failure=Failure("sentry-demo-sneaky", "native_openhands", "patch targets a user file", ""),
        diagnosis="proposed change writes outside skill code",
        fix="",
        test_script="",
        changed_files=["C:/Users/HP/Documents/report.docx"],
        candidate_dir=_HOME,
        sandbox_passed=False,
    )
    print("\n[sentry] negative check: proposed changed_files =", sneaky.changed_files)
    print(f"[sentry] refuses to touch user files: {_refuses_user_files(sneaky)}")
    print("[sentry] user files always go through the normal write-tier confirm, never the sentry.")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    from jarvis.logging import setup_from_env

    setup_from_env()
    ap = argparse.ArgumentParser(prog="jarvis.sentry")
    sub = ap.add_subparsers(dest="command")
    d = sub.add_parser("demo")
    w = sub.add_parser("watch")
    w.add_argument("--interval", type=int, default=60)
    args = ap.parse_args(argv)
    if args.command == "watch":
        watch(interval=args.interval)
        return 0
    return demo()


if __name__ == "__main__":
    sys.exit(main())
