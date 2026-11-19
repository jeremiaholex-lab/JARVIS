"""JARVIS Phase 4 runner.

Phase 4 entry points, all JARVIS-side glue around the OpenJarvis runtime:
  demo      — done-test #1: watch the pre-execution auditor hold a risky action
  skill     — done-test #2: forge a skill, sandbox-test it, approval gate
  sentry    — auto-heal: diagnose failures, patch skill code in the sandbox
  rag       — index user folders + cited search (done-test #3 material)
  ask       — run a secured agent turn: tiers + auditor gate + RAG tool

Run under the runtime venv python, from the JARVIS root.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Pin the runtime state home BEFORE anything imports openjarvis and resolves a
# default path. Without it, `get_config_dir()` falls back to ~/.openjarvis, so
# phase4 would read traces the orb wrote to JARVIS/.openjarvis and find
# nothing — "no failure signal to learn from" while the evidence sits one
# directory away. phase3.py already did this; phase4 did not.
os.environ.setdefault("OPENJARVIS_HOME", str(ROOT / ".openjarvis"))
# Ensure the OpenJarvis runtime is on the path so `openjarvis.*` imports work
# (the runtime lives at openjarvis_runtime/src/, not at the project root).
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "openjarvis_runtime" / "src"))

# Module logger
_logger = logging.getLogger(__name__)


def _register_rag_tool() -> None:
    """JARVIS-side tool registration, mirroring how Phase 3 wired
    phase3_projects (import-for-registration via the ToolRegistry)."""
    import jarvis.rag.tool  # noqa: F401 - registers jarvis_rag_search


def demo(args) -> int:
    from jarvis.security.launcher import demo as sec_demo

    return sec_demo(no_llm=args.no_llm)


def skill(args) -> int:
    from jarvis.skills.pipeline import run_pipeline

    return run_pipeline(args.gap or "no way to count words and lines in a file",
                        no_llm=args.no_llm, auto_approve=args.auto_approve)


def sentry(args) -> int:
    from jarvis.sentry.runner import demo as sentry_demo

    return sentry_demo()


def optimise(args) -> int:
    """§6.6 step 4: improve an installed skill from its own trace history."""
    from jarvis.security.launcher import _build_engines
    from jarvis.skills.optimize import optimize_skill

    skill = args.skill
    if not skill:
        from openjarvis.core.paths import get_config_dir

        skills_dir = Path(get_config_dir()) / "skills"
        found = sorted(p.name for p in skills_dir.glob("*") if (p / "SKILL.md").is_file()) \
            if skills_dir.is_dir() else []
        if not found:
            print("no installed skills to optimise yet (forge one first: python phase4.py skill)")
            return 0
        print("installed skills: " + ", ".join(found))
        print("re-run with --skill <name>")
        return 0
    local, cloud = _build_engines()
    return optimize_skill(
        skill, local_engine=local, cloud_engine=cloud,
        auto_approve=args.auto_approve, dry_run=args.dry_run,
    )


def rag(args) -> int:
    from jarvis.rag.setup import ask as rag_ask, setup as rag_setup

    import json

    if args.ask:
        print(json.dumps(rag_ask(args.ask, top_k=args.top_k), indent=2))
    else:
        print(json.dumps(rag_setup(args.folders or None, force=args.force), indent=2))
    return 0


def secured_ask(args) -> int:
    _register_rag_tool()
    from jarvis.security.launcher import install_gate

    import importlib

    ask_cli = importlib.import_module("openjarvis.cli.ask")

    # Phase 4 config adds jarvis_rag_search to the enabled tools list.
    if "OPENJARVIS_CONFIG" not in os.environ:
        os.environ["OPENJARVIS_CONFIG"] = str(ROOT / "jarvis-phase4.toml")
    install_gate(use_llm=not args.no_llm)
    params: list[str] = []
    if args.model:
        params += ["--model", args.model]
    params += ["--no-stream", *args.query]
    try:
        ask_cli.ask(params, standalone_mode=False)  # click Command __call__
    except SystemExit as exc:
        code = exc.code
        _logger.info("secured ask finished (result=%r)", code)
        return 0 if code in (None, 0) else 1
    return 0


def main(argv=None) -> int:
    from jarvis.logging import setup_from_env

    setup_from_env()
    ap = argparse.ArgumentParser(prog="jarvis-phase4")
    sub = ap.add_subparsers(dest="command", required=True)

    d = sub.add_parser("demo", help="done-test #1: auditor holds a risky action")
    d.add_argument("--no-llm", action="store_true")

    s = sub.add_parser("skill", help="done-test #2: forge -> sandbox-test -> approve")
    s.add_argument("gap", nargs="?", default="")
    s.add_argument("--no-llm", action="store_true", help="use the static demo proposal")
    s.add_argument("--auto-approve", action="store_true", help="test mode: skip typing 'approve'")

    sub.add_parser("sentry", help="auto-heal demo (diagnose + sandbox patch + refusal check)")

    o = sub.add_parser("optimise", help="§6.6 step 4: improve a skill from trace evidence")
    o.add_argument("--skill", help="installed skill name (omit to list them)")
    o.add_argument("--dry-run", action="store_true", help="analyse only; no proposal generated")
    o.add_argument("--auto-approve", action="store_true", help="test mode: skip typing 'approve'")

    r = sub.add_parser("rag", help="index user folders and/or cited search")
    r.add_argument("folders", nargs="*")
    r.add_argument("--force", action="store_true")
    r.add_argument("--ask", help="search the index with this query instead of indexing")
    r.add_argument("--top-k", type=int, default=5)

    a = sub.add_parser("ask", help="run a secured agent turn (tiers + auditor + RAG tool)")
    a.add_argument("query", nargs="+")
    a.add_argument("--model")
    a.add_argument("--no-llm", action="store_true", help="deterministic auditor only")

    args = ap.parse_args(argv)
    return {"demo": demo, "skill": skill, "sentry": sentry, "rag": rag,
            "ask": secured_ask, "optimise": optimise}[args.command](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
