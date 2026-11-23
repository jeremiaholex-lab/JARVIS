from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server


def test_desktop_control_builder_returns_working_adapter():
    from jarvis.desktop.win32_control import DesktopControl, Win32DesktopControl
    control = server.build_desktop_control()
    # The orb settle path must construct the real Win32 adapter. The
    # Protocol itself cannot be instantiated — that was the historical
    # crash: typing "yes, do it" dropped the socket before the executor
    # ever ran.
    assert isinstance(control, Win32DesktopControl)
    assert isinstance(control, DesktopControl)
    assert type(control) is not DesktopControl


def test_builder_constructs_a_fresh_adapter_each_call():
    control_a = server.build_desktop_control()
    control_b = server.build_desktop_control()
    assert control_a is not control_b


def test_settle_ok_result_does_not_report_blocked():
    """A successful execution must not carry the 'blocked' fallback reason.

    The executor's ok payload has no error/reason key, so the settle path's
    `... or "blocked"` fallback leaked into every successful desktop_result.
    """
    assert server.desktop_result_detail({"status": "ok", "action_id": "da_ok"}) == ""
    assert server.desktop_result_detail(
        {"status": "failed", "error": "boom"}) == "boom"
    assert server.desktop_result_detail(
        {"status": "cancelled", "reason": "hud stop"}) == "hud stop"
    assert server.desktop_result_detail({"status": "failed"}) == "blocked"
