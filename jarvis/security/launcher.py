"""In-process secure launcher — installs the JARVIS ConfirmGate into the
OpenJarvis `ask` flow and runs it. Runs under the runtime venv python.

Design: zero framework edits. The CLI module's confirm callback is a module
attribute consumed at call time, so patching it here (JARVIS-side glue)
rewires the entire tool-execution confirmation path to
tiers -> auditor -> confirm-style -> audit chain.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # .../JARVIS

# Module logger
_logger = logging.getLogger(__name__)


def _load_env(path: Path) -> dict[str, str]:
    cfg = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def _build_engines():
    """Local Ollama first, Agnes cloud for escalation. Either may be None
    (auditor falls back to its deterministic layer)."""
    local, cloud = None, None
    try:
        from openjarvis.engine.ollama import OllamaEngine

        local = OllamaEngine()
    except Exception as exc:
        _logger.warning("OllamaEngine unavailable, auditor will fall back: %s", exc)
    env = _load_env(ROOT / ".env")
    if env.get("AGNES_API_KEY"):
        try:
            from openjarvis.engine.openai_compat_engines import OpenAICompatEngine

            base = env.get("AGNES_BASE_URL", "").rstrip("/").removesuffix("/v1")
            cloud = OpenAICompatEngine(host=base, api_key=env["AGNES_API_KEY"])
        except Exception as exc:
            _logger.warning("cloud engine unavailable: %s", exc)
    return local, cloud


def install_gate(*, use_llm: bool = True, input_fn=None, print_fn=None) -> dict:
    """Patch the CLI's confirm callback with the JARVIS gate. Returns the
    wiring so tests/demos can inspect or drive it."""
    import openjarvis.cli.ask as ask_cli
    from openjarvis.security.audit import AuditLogger

    from jarvis.security.auditor import Auditor
    from jarvis.security.gate import ConfirmGate

    local, cloud = _build_engines()
    home = Path(os.environ.get("OPENJARVIS_HOME", str(ROOT / ".openjarvis")))
    audit = AuditLogger(db_path=home / "audit.db")
    auditor = Auditor(local_engine=local, cloud_engine=cloud, use_llm=use_llm)
    gate = ConfirmGate(auditor, audit_logger=audit, input_fn=input_fn or input, print_fn=print_fn or print)
    ask_cli._confirm_tool_action = gate
    return {"gate": gate, "auditor": auditor, "audit": audit}


def run_secured_ask(query: str, *, model: str | None = None, no_llm: bool = False) -> int:
    install_gate(use_llm=not no_llm)
    import openjarvis.cli.ask as ask_cli

    params: list[str] = []
    if model:
        params += ["--model", model]
    params += ["--no-stream", *query.split()]
    try:
        result = ask_cli.ask.main(params, standalone_mode=False)
    except SystemExit as exc:  # pragma: no cover - defensive
        result = exc.code
    _logger.info("secured ask finished (result=%r; auditor LLM %s).",
                 result, "on" if not no_llm else "deterministic-only")
    return 0 if result in (None, 0) else 1


def demo(*, no_llm: bool = False) -> int:
    """Done-test #1 demo: watch the auditor hold/pass/flag sample actions,
    scripted 'user' answers, then print the audit chain tail."""
    from jarvis.security.tiers import PlannedAction, classify

    samples = [
        ("read a file", PlannedAction("file_read", {"path": "docs/README.md"})),
        ("write a harmless file", PlannedAction("file_write", {"path": "notes/todo.txt", "content": "x"})),
        ("write touching .env (risky)", PlannedAction("file_write", {"path": ".env", "content": "API_KEY=..."})),
        ("destructive shell", PlannedAction("shell_exec", {"command": "rm -rf build/ && git reset --hard"})),
    ]

    def fake_input(prompt: str) -> str:
        # Simulated user: approve normal writes, refuse the held write,
        # type the magic phrase for the destructive one.
        if "Still allow" in prompt:
            print("    (user) n")
            return "n"
        if "[typed]" in prompt:
            print("    (user) yes, do it")
            return "yes, do it"
        print("    (user) y")
        return "y"

    wiring = install_gate(use_llm=not no_llm, input_fn=fake_input)
    gate = wiring["gate"]

    print("=" * 64)
    print("PHASE 4 DEMO — tiers + pre-execution auditor (LLM: "
          f"{'on' if not no_llm else 'off'})")
    print("=" * 64)
    for label, action in samples:
        print(f"\n--- planned: {label}")
        verdict = gate.auditor.judge(action)
        print(f"    tier={classify(action).value} auditor={verdict.decision.value} "
              f"({verdict.reason}) engine={verdict.engine_used}")
        prompt = f"Allow execution of tool '{action.tool}' with args {json.dumps(dict(action.params))}?"
        approved = gate(prompt)
        print(f"    user decision: {'APPROVED' if approved else 'DENIED'}")

    print("\n--- audit chain (last 4)")
    audit = wiring["audit"]
    for event in audit.query(limit=4):
        print(f"    [{event.event_type.value}] {event.content_preview[:72]} -> {event.action_taken}")
    ok, at = audit.verify_chain()
    print(f"    chain tail: {audit.tail_hash()[:16]}… verification: {'VALID' if ok else 'INVALID at row ' + str(at)}")
    return 0


def main(argv: list[str]) -> int:
    import argparse

    from jarvis.logging import setup_from_env

    setup_from_env()
    ap = argparse.ArgumentParser(prog="jarvis-secure")
    sub = ap.add_subparsers(dest="command", required=True)
    d = sub.add_parser("demo")
    d.add_argument("--no-llm", action="store_true")
    a = sub.add_parser("ask")
    a.add_argument("query", nargs="+")
    a.add_argument("--model")
    a.add_argument("--no-llm", action="store_true")
    args = ap.parse_args(argv)

    if args.command == "demo":
        return demo(no_llm=args.no_llm)
    return run_secured_ask(" ".join(args.query), model=args.model, no_llm=args.no_llm)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
