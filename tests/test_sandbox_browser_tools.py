"""Tests for sandbox_execute and browser tools (sandbox_browser_tools.py)."""
from __future__ import annotations

import json
import pytest


def test_sandbox_execute_basic():
    """Basic sandbox execution: prints and arithmetic."""
    from jarvis.agent.sandbox_browser_tools import sandbox_execute

    result = json.loads(sandbox_execute("print('hello'); x = 2 + 3; print(x)"))
    assert result["passed"] is True
    assert "hello" in result["stdout"]
    assert "5" in result["stdout"]


def test_sandbox_execute_math_json():
    """Sandbox can import allowed modules (json, math, time)."""
    from jarvis.agent.sandbox_browser_tools import sandbox_execute

    result = json.loads(sandbox_execute(
        "import json, math, time; "
        "print(json.dumps({'pi': math.pi})); "
        "print(time.time() > 0)"
    ))
    assert result["passed"] is True
    assert "pi" in result["stdout"]


def test_sandbox_execute_blocks_unsafe_imports():
    """AST validation blocks disallowed imports (os, sys, subprocess, etc.)."""
    from jarvis.agent.sandbox_browser_tools import sandbox_execute

    # Try to import os - should be blocked
    result = json.loads(sandbox_execute("import os; print(os.listdir('.'))"))
    assert result["passed"] is False
    assert "blocked by code validation" in result["error"].lower()


def test_sandbox_execute_blocks_eval_exec():
    """AST validation blocks eval/exec."""
    from jarvis.agent.sandbox_browser_tools import sandbox_execute

    result = json.loads(sandbox_execute("eval('1+1')"))
    assert result["passed"] is False
    assert "blocked by code validation" in result["error"].lower()

    result = json.loads(sandbox_execute("exec('print(1)')"))
    assert result["passed"] is False
    assert "blocked by code validation" in result["error"].lower()


def test_sandbox_execute_blocks_open():
    """AST validation blocks open() and file operations."""
    from jarvis.agent.sandbox_browser_tools import sandbox_execute

    result = json.loads(sandbox_execute("open('/etc/passwd').read()"))
    assert result["passed"] is False
    assert "blocked by code validation" in result["error"].lower()


def test_sandbox_execute_blocks_private_attrs():
    """AST validation blocks private attribute access."""
    from jarvis.agent.sandbox_browser_tools import sandbox_execute

    result = json.loads(sandbox_execute("print(x.__dict__)"))
    assert result["passed"] is False
    assert "blocked by code validation" in result["error"].lower()


def test_sandbox_execute_timeout():
    """Long-running code gets timed out."""
    from jarvis.agent.sandbox_browser_tools import sandbox_execute

    result = json.loads(sandbox_execute("import time; time.sleep(10)", timeout=1))
    assert result["passed"] is False
    assert result["timed_out"] is True


def test_sandbox_execute_invalid_syntax():
    """Syntax errors are caught before execution."""
    from jarvis.agent.sandbox_browser_tools import sandbox_execute

    result = json.loads(sandbox_execute("print('missing paren"))
    assert result["passed"] is False
    assert "syntaxerror" in result["error"].lower()


def test_sandbox_execute_empty_code():
    """Empty code returns error."""
    from jarvis.agent.sandbox_browser_tools import sandbox_execute

    result = json.loads(sandbox_execute(""))
    assert result["passed"] is False
    assert "no code provided" in result["error"].lower()


def test_sandbox_execute_code_too_long():
    """Overlong code is rejected."""
    from jarvis.agent.sandbox_browser_tools import sandbox_execute, _MAX_CODE_CHARS

    long_code = "print(1)\n" * (_MAX_CODE_CHARS // 8 + 1)
    result = json.loads(sandbox_execute(long_code))
    assert result["passed"] is False
    assert "code too long" in result["error"].lower()


@pytest.mark.skip(reason="Requires Playwright and headless browser - run manually")
def test_browser_navigate():
    """Browser navigate to a simple page."""
    from jarvis.agent.sandbox_browser_tools import browser_navigate

    result = json.loads(browser_navigate("https://example.com", wait_for="load"))
    assert result["success"] is True
    assert "content" in result
    assert "Example Domain" in result["content"]


@pytest.mark.skip(reason="Requires Playwright and headless browser - run manually")
def test_browser_click_extract():
    """Click and extract sequence."""
    from jarvis.agent.sandbox_browser_tools import (
        browser_navigate, browser_click, browser_extract, browser_type
    )

    # Navigate to a simple page
    result = json.loads(browser_navigate("https://example.com"))
    assert result["success"] is True

    # Extract text
    result = json.loads(browser_extract("body", "text"))
    assert result["success"] is True
    assert "Example Domain" in result["content"]


@pytest.mark.skip(reason="Requires Playwright and headless browser - run manually")
def test_browser_screenshot():
    """Screenshot captures page dimensions."""
    from jarvis.agent.sandbox_browser_tools import browser_navigate, browser_screenshot

    result = json.loads(browser_navigate("https://example.com"))
    assert result["success"] is True

    result = json.loads(browser_screenshot(full_page=False))
    assert result["success"] is True
    metadata = result.get("metadata", {})
    assert "width" in metadata or "screenshot_bytes" in metadata


if __name__ == "__main__":
    pytest.main([__file__, "-v"])