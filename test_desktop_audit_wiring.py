"""Production wiring tests: the orb must inject the desktop audit logger.

The binding constraint is that every desktop proposal, approval, denial,
execution, failure, and cancellation is audited. The desktop module only
writes to the trail when a logger is injected, so the orb's startup must
call ``configure_desktop_audit`` exactly as it does for writes and file edits.
"""
from __future__ import annotations

import ast
from pathlib import Path

SERVER = Path(__file__).resolve().parent / "orb" / "server.py"


def _source() -> str:
    return SERVER.read_text(encoding="utf-8")


def test_orb_wires_desktop_audit_logger():
    """orb/server.py injects an AuditLogger into jarvis.desktop.actions."""
    src = _source()
    assert "configure_desktop_audit" in src, (
        "orb/server.py never calls configure_desktop_audit; desktop "
        "transitions would be silently unaudited in production"
    )


def test_orb_desktop_audit_uses_the_shared_audit_db():
    """The desktop logger writes the same audit.db the confirm gate uses."""
    tree = ast.parse(_source())
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "_build_desktop_audit":
            continue
        calls = [
            ast.unparse(call.func) for call in ast.walk(node) if isinstance(call, ast.Call)
        ]
        assert any("AuditLogger" in c for c in calls), (
            "_build_desktop_audit must construct an AuditLogger"
        )
        assert any("audit_db" in c for c in calls), (
            "_build_desktop_audit must point at audit_db() so desktop events "
            "land in the same hash-chained trail as writes and file edits"
        )
        found = True
        break
    assert found, "orb/server.py must define _build_desktop_audit"


def test_orb_calls_configure_desktop_audit_at_startup():
    """The injection happens once when the host builds its toolchain."""
    tree = ast.parse(_source())
    for node in ast.walk(tree):
        # The helper that performs the injection; skip its own body.
        if isinstance(node, ast.FunctionDef) and node.name == "_build_desktop_audit":
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            calls = [
                ast.unparse(call.func) for call in ast.walk(node) if isinstance(call, ast.Call)
            ]
            hit = any(
                c == "configure_desktop_audit"
                or c.endswith("configure_desktop_audit")
                or c.endswith("_build_desktop_audit")
                for c in calls
            )
            if hit:
                # The call site must be a host setup method, not the helper.
                assert node.name == "__init__" or node.name.startswith("_build"), (
                    f"desktop audit wired from unexpected place: {node.name}"
                )
                return
    raise AssertionError(
        "no startup call site found for the desktop audit wiring outside "
        "_build_desktop_audit"
    )
