"""Sandbox execution and browser automation, wrapped for the JARVIS agent.

Sandbox: code is AST-validated first (the same structure-based filter the
standalone code interpreter uses), then executed through
``jarvis.skills.sandbox_test.run_skill_tests`` — Docker-first with a
restricted-process fallback. The ``isolation`` field in the result says which
boundary actually ran; callers must not treat "fallback" as container
isolation.

Browser: thin JSON wrappers over the vendored Playwright tools in
``openjarvis.tools.browser``. They share that module's single browser session,
so a navigate/click/extract sequence sees the same page. The session is a
fresh headless Chromium with no persisted logins.
"""
from __future__ import annotations

import json
from typing import Any

_MAX_CODE_CHARS = 20_000


def _result_json(result: Any) -> str:
    """Serialize a ToolResult-like object without leaking giant payloads."""
    metadata = dict(getattr(result, "metadata", None) or {})
    if "screenshot_base64" in metadata:
        b64 = metadata.pop("screenshot_base64") or ""
        metadata["screenshot_bytes"] = len(b64) * 3 // 4
    return json.dumps(
        {
            "success": bool(getattr(result, "success", False)),
            "content": str(getattr(result, "content", "")),
            "metadata": metadata,
        },
        ensure_ascii=False,
        default=str,
    )


# ---------------------------------------------------------------------------
# sandbox
# ---------------------------------------------------------------------------

def sandbox_execute(code: str, timeout: int = 30) -> str:
    """Run Python in the isolated sandbox and report what it printed.

    Validation happens before anything is written to disk. Only json/math/time
    may be imported; eval/exec/open/getattr and private-attribute walks are
    rejected. The subprocess is then run under Docker when available, else as
    a restricted process with a sanitized environment and a memory watchdog.
    """
    if not isinstance(code, str) or not code.strip():
        return json.dumps({"error": "no code provided", "passed": False})
    if len(code) > _MAX_CODE_CHARS:
        return json.dumps({
            "error": f"code too long ({len(code)} chars, max {_MAX_CODE_CHARS})",
            "passed": False,
        })

    from openjarvis.tools.code_interpreter import UnsafeCodeError, _validate_ast

    try:
        _validate_ast(code)
    except UnsafeCodeError as exc:
        return json.dumps({"error": f"blocked by code validation: {exc}", "passed": False})
    except SyntaxError as exc:
        return json.dumps({"error": f"SyntaxError: {exc}", "passed": False})

    from jarvis.skills.sandbox_test import run_skill_tests

    seconds = max(1, min(int(timeout or 30), 60))
    result = run_skill_tests(code, timeout=float(seconds))
    return json.dumps(
        {
            "passed": result.passed,
            "isolation": result.isolation,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
            "killed_for_memory": result.killed_for_memory,
            "returncode": result.returncode,
            "note": result.note,
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# browser
# ---------------------------------------------------------------------------

def browser_navigate(url: str, wait_for: str = "load") -> str:
    from openjarvis.tools.browser import BrowserNavigateTool

    return _result_json(BrowserNavigateTool().execute(url=url, wait_for=wait_for))


def browser_click(selector: str, by_text: bool = False) -> str:
    from openjarvis.tools.browser import BrowserClickTool

    return _result_json(BrowserClickTool().execute(selector=selector, by_text=by_text))


def browser_type(selector: str, text: str, clear: bool = True) -> str:
    from openjarvis.tools.browser import BrowserTypeTool

    return _result_json(BrowserTypeTool().execute(selector=selector, text=text, clear=clear))


def browser_screenshot(full_page: bool = False) -> str:
    from openjarvis.tools.browser import BrowserScreenshotTool

    # No path parameter: the tool writes nothing to the host filesystem.
    return _result_json(BrowserScreenshotTool().execute(full_page=full_page))


def browser_extract(selector: str = "body", extract_type: str = "text") -> str:
    from openjarvis.tools.browser import BrowserExtractTool

    return _result_json(
        BrowserExtractTool().execute(selector=selector, extract_type=extract_type)
    )


__all__ = [
    "sandbox_execute",
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_screenshot",
    "browser_extract",
]
