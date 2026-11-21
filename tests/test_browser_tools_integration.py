"""Integration tests for browser tool wrappers in jarvis.agent.sandbox_browser_tools."""
from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from openjarvis.core.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Helpers — mock the _session before importing tools
# ---------------------------------------------------------------------------


def _make_mock_page():
    """Create a fully mocked Playwright page object."""
    page = MagicMock()
    page.title.return_value = "Test Page"
    page.inner_text.return_value = "Hello World"
    page.screenshot.return_value = b"\x89PNG\x00fake-screenshot-data"

    response = MagicMock()
    response.status = 200
    page.goto.return_value = response

    page.click.return_value = None
    page.get_by_text.return_value = MagicMock()
    page.fill.return_value = None
    page.type.return_value = None
    page.eval_on_selector_all.return_value = []

    return page


def _make_mock_session(page=None):
    """Create a mocked session whose .page returns a mock page."""
    if page is None:
        page = _make_mock_page()
    session = MagicMock()
    type(session).page = PropertyMock(return_value=page)
    return session


# ---------------------------------------------------------------------------
# TestBrowserNavigateWrapper
# ---------------------------------------------------------------------------


class TestBrowserNavigateWrapper:
    """Tests for browser_navigate wrapper function."""

    def test_navigate_success(self):
        """Navigate returns title and content on success."""
        from jarvis.agent.sandbox_browser_tools import browser_navigate

        page = _make_mock_page()
        page.title.return_value = "Example Domain"
        page.inner_text.return_value = "Example Domain page"
        session = _make_mock_session(page)

        with patch("openjarvis.tools.browser._session", session):
            with patch("openjarvis.security.ssrf.check_ssrf", return_value=None):
                result = json.loads(browser_navigate("https://example.com"))

        assert result["success"] is True
        assert "Example Domain" in result["content"]
        assert result["metadata"]["url"] == "https://example.com"

    def test_navigate_ssrf_blocked(self):
        """Navigate blocks SSRF attempts."""
        from jarvis.agent.sandbox_browser_tools import browser_navigate

        mock_ssrf_module = MagicMock()
        mock_ssrf_module.check_ssrf.return_value = (
            "Blocked host: 169.254.169.254 (cloud metadata endpoint)"
        )

        with patch.dict(
            "sys.modules",
            {"openjarvis.security.ssrf": mock_ssrf_module},
        ):
            result = json.loads(browser_navigate("http://169.254.169.254/latest/meta-data/"))

        assert result["success"] is False
        assert "SSRF blocked" in result["content"]


# ---------------------------------------------------------------------------
# TestBrowserClickWrapper
# ---------------------------------------------------------------------------


class TestBrowserClickWrapper:
    """Tests for browser_click wrapper function."""

    def test_click_success_css(self):
        """Click returns success when element is found via CSS."""
        from jarvis.agent.sandbox_browser_tools import browser_click

        page = _make_mock_page()
        page.frames = [page]
        match = MagicMock()
        match.is_visible.return_value = True
        match.is_enabled.return_value = True
        page.locator.return_value.count.return_value = 1
        page.locator.return_value.nth.return_value = match
        page.get_by_text.return_value.count.return_value = 0
        page.get_by_role.return_value.count.return_value = 0
        session = _make_mock_session(page)

        with patch("openjarvis.tools.browser._session", session):
            result = json.loads(browser_click("#submit-btn"))

        assert result["success"] is True
        assert "Clicked element" in result["content"]
        assert result["metadata"]["selector"] == "#submit-btn"

    def test_click_success_by_text(self):
        """Click returns success when clicking by visible text."""
        from jarvis.agent.sandbox_browser_tools import browser_click

        page = _make_mock_page()
        page.frames = [page]
        match = MagicMock()
        match.is_visible.return_value = True
        match.is_enabled.return_value = True
        page.get_by_text.return_value.count.return_value = 1
        page.get_by_text.return_value.nth.return_value = match
        session = _make_mock_session(page)

        with patch("openjarvis.tools.browser._session", session):
            result = json.loads(browser_click("Sign In", by_text=True))

        assert result["success"] is True
        assert page.get_by_text.call_count >= 1
        match.click.assert_called_once_with(timeout=8000)
        assert result["metadata"]["by_text"] is True

    def test_click_error(self):
        """Click returns error when element not found."""
        from jarvis.agent.sandbox_browser_tools import browser_click

        page = _make_mock_page()
        page.frames = [page]
        match = MagicMock()
        match.is_visible.return_value = True
        match.is_enabled.return_value = True
        match.click.side_effect = Exception("Element not found")
        page.locator.return_value.count.return_value = 1
        page.locator.return_value.nth.return_value = match
        page.get_by_text.return_value.count.return_value = 0
        page.get_by_role.return_value.count.return_value = 0
        session = _make_mock_session(page)

        with patch("openjarvis.tools.browser._session", session):
            result = json.loads(browser_click("#nonexistent"))

        assert result["success"] is False
        assert "Click error" in result["content"]


# ---------------------------------------------------------------------------
# TestBrowserTypeWrapper
# ---------------------------------------------------------------------------


class TestBrowserTypeWrapper:
    """Tests for browser_type wrapper function."""

    def test_type_success(self):
        """Type returns success when text is entered."""
        from jarvis.agent.sandbox_browser_tools import browser_type

        page = _make_mock_page()
        page.fill.return_value = None
        page.type.return_value = None
        session = _make_mock_session(page)

        with patch("openjarvis.tools.browser._session", session):
            result = json.loads(browser_type("#input", "test"))

        assert result["success"] is True
        page.fill.assert_called_once_with("#input", "test")

    def test_type_no_selector(self):
        """Type returns error when selector is missing."""
        from jarvis.agent.sandbox_browser_tools import browser_type

        result = json.loads(browser_type("", "test"))
        assert result["success"] is False
        assert "No selector" in result["content"]

    def test_type_no_text(self):
        """Type returns error when text is missing."""
        from jarvis.agent.sandbox_browser_tools import browser_type

        result = json.loads(browser_type("#input", ""))
        assert result["success"] is False
        assert "No text" in result["content"]


# ---------------------------------------------------------------------------
# TestBrowserScreenshotWrapper
# ---------------------------------------------------------------------------


class TestBrowserScreenshotWrapper:
    """Tests for browser_screenshot wrapper function."""

    def test_screenshot_success(self):
        """Screenshot returns metadata on success."""
        from jarvis.agent.sandbox_browser_tools import browser_screenshot

        page = _make_mock_page()
        session = _make_mock_session(page)

        with patch("openjarvis.tools.browser._session", session):
            result = json.loads(browser_screenshot(full_page=False))

        assert result["success"] is True
        assert "Screenshot taken" in result["content"]
        assert "full page" not in result["content"]
        # Verify screenshot_bytes in metadata (screenshot_base64 is converted to length)
        assert "screenshot_bytes" in result["metadata"]
        assert isinstance(result["metadata"]["screenshot_bytes"], int)

    def test_screenshot_full_page(self):
        """Screenshot with full_page returns correct metadata."""
        from jarvis.agent.sandbox_browser_tools import browser_screenshot

        page = _make_mock_page()
        session = _make_mock_session(page)

        with patch("openjarvis.tools.browser._session", session):
            result = json.loads(browser_screenshot(full_page=True))

        assert result["success"] is True
        assert "full page" in result["content"]


# ---------------------------------------------------------------------------
# TestBrowserExtractWrapper
# ---------------------------------------------------------------------------


class TestBrowserExtractWrapper:
    """Tests for browser_extract wrapper function."""

    def test_extract_text_success(self):
        """Extract returns text content on success."""
        from jarvis.agent.sandbox_browser_tools import browser_extract

        page = _make_mock_page()
        page.inner_text.return_value = "Page content here"
        session = _make_mock_session(page)

        with patch("openjarvis.tools.browser._session", session):
            result = json.loads(browser_extract("body", "text"))

        assert result["success"] is True
        assert "Page content here" in result["content"]

    def test_extract_links_success(self):
        """Extract returns links on success."""
        from jarvis.agent.sandbox_browser_tools import browser_extract

        page = _make_mock_page()
        page.eval_on_selector_all.return_value = [
            {"href": "https://example.com/page1", "text": "Page 1"},
        ]
        session = _make_mock_session(page)

        with patch("openjarvis.tools.browser._session", session):
            result = json.loads(browser_extract("body", "links"))

        assert result["success"] is True
        assert "[Page 1](https://example.com/page1)" in result["content"]

    def test_extract_tables_success(self):
        """Extract returns tables on success."""
        from jarvis.agent.sandbox_browser_tools import browser_extract

        page = _make_mock_page()
        page.eval_on_selector_all.return_value = [
            "Name\tAge\nAlice\t30",
        ]
        session = _make_mock_session(page)

        with patch("openjarvis.tools.browser._session", session):
            result = json.loads(browser_extract("body", "tables"))

        assert result["success"] is True
        assert "Alice" in result["content"]


# ---------------------------------------------------------------------------
# TestBrowserSessionWrapper
# ---------------------------------------------------------------------------


class TestBrowserSessionWrapper:
    """Tests for browser session management."""

    def test_session_close_resets_state(self):
        """Session close should reset internal state."""
        from openjarvis.tools.browser import _BrowserSession

        session = _BrowserSession()
        session._playwright = MagicMock()
        session._browser = MagicMock()
        session._page = MagicMock()

        session.close()

        assert session._playwright is None
        assert session._browser is None
        assert session._page is None

    def test_session_close_noop_when_not_initialized(self):
        """Session close should not raise when not initialized."""
        from openjarvis.tools.browser import _BrowserSession

        session = _BrowserSession()
        session.close()  # Should not raise
        assert session._playwright is None


# ---------------------------------------------------------------------------
# TestBrowserIntegration
# ---------------------------------------------------------------------------


class TestBrowserIntegration:
    """Integration tests for browser tool wrappers."""

    def test_all_tools_registered(self):
        """All browser tools should be registered in ToolRegistry."""
        from openjarvis.tools.browser import (
            BrowserNavigateTool,
            BrowserClickTool,
            BrowserTypeTool,
            BrowserScreenshotTool,
            BrowserExtractTool,
        )

        tools = [
            "browser_navigate",
            "browser_click",
            "browser_type",
            "browser_screenshot",
            "browser_extract",
        ]
        for key in tools:
            assert ToolRegistry.contains(key), f"Tool {key} not registered"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])