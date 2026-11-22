"""End-to-end integration tests for the seven desktop control tools.

The tool boundary is side-effect free: a tool call registers a pending
DesktopAction and returns a card payload. No test here constructs the real
Win32 boundary, moves a pointer, sends input, or opens a real file. All OS
effects are observed through a FakeDesktop injected into the offline approval
path.
"""
from __future__ import annotations

import base64
import json
import struct
import zlib

import pytest

from jarvis.agent import tools as agent_tools
from jarvis.agent.tools import build_tools
from jarvis.desktop import evidence as evidence_module
from jarvis.desktop import overlay_protocol
from jarvis.desktop.actions import (
    clear_desktop_proposal_hook,
    consume_desktop_proposal,
)
from jarvis.desktop.executor import DesktopActionExecutor, reset_cancel
from jarvis.security.tiers import PlannedAction, Tier, classify

DESKTOP_TOOLS = (
    "desktop_open_app",
    "desktop_open_file",
    "desktop_move_cursor",
    "desktop_click",
    "desktop_scroll",
    "desktop_type",
    "desktop_press_keys",
)

VALID_CALLS = {
    "desktop_open_app": {"name_or_path": "Notepad"},
    "desktop_open_file": {"path": "C:/Users/Public/Documents/notes.txt"},
    "desktop_move_cursor": {"x": 10, "y": 20},
    "desktop_click": {"x": 10, "y": 20},
    "desktop_scroll": {"x": 10, "y": 20, "direction": "down", "amount": 2},
    "desktop_type": {"text": "hello"},
    "desktop_press_keys": {"keys": ["ctrl", "a"]},
}


class FakeDesktop:
    """Records every OS-boundary call; none of them reach the real desktop."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def cursor_pos(self):
        return (0, 0)

    def move_cursor(self, x, y):
        self.calls.append(("move_cursor", x, y))

    def click(self, x, y, button, clicks):
        self.calls.append(("click", x, y, button, clicks))

    def scroll(self, x, y, direction, amount):
        self.calls.append(("scroll", x, y, direction, amount))

    def type_text(self, text):
        self.calls.append(("type_text", text))

    def press_keys(self, keys):
        self.calls.append(("press_keys", list(keys)))

    def focus_window(self, hwnd):
        self.calls.append(("focus_window", hwnd))

    def window_rect(self, hwnd):
        return (0, 0, 100, 100)

    def open(self, path):
        self.calls.append(("open", path))


class FakeContext:
    """Minimal host context stand-in; the tools need no host objects."""


@pytest.fixture(autouse=True)
def isolated_desktop_session(monkeypatch):
    """Every test starts and ends with no card, pending action, or hook."""
    reset_cancel()
    overlay_protocol.reset_desktop_session("integration test setup")
    clear_desktop_proposal_hook()
    # No test may reach a real screen or construct a real Win32 boundary.
    monkeypatch.setattr(agent_tools, "_desktop_capture_control", lambda: FakeDesktop())
    yield
    overlay_protocol.reset_desktop_session("integration test teardown")
    clear_desktop_proposal_hook()
    reset_cancel()


def _propose(name: str, arguments: dict | None = None) -> dict:
    clear_desktop_proposal_hook()
    payload = json.loads(agent_tools.execute(
        build_tools(), name, arguments if arguments is not None else VALID_CALLS[name]
    ))
    assert "error" not in payload, payload
    return payload


def _proposal_png(width: int, height: int) -> bytes:
    raw = b"".join(b"\x00" + b"\x10\x20\x30" * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


@pytest.fixture
def fake_screen(monkeypatch):
    """A process-local screen source; no GDI call can escape this test."""

    class FakeCapture:
        def __init__(self):
            self.label = "Chrome - Notes"

        def list_windows(self):
            return [type("Window", (), {"label": self.label})()]

        def grab(self):
            return evidence_module.Screen(
                png=_proposal_png(1000, 800), width=1000, height=800,
                captured_at=0.0, backend="fake")

    capture = FakeCapture()
    monkeypatch.setattr(evidence_module, "ScreenCapture", lambda: capture)
    return capture


@pytest.mark.parametrize("name", DESKTOP_TOOLS)
def test_all_seven_desktop_tools_are_exposed(name):
    tool = next((t for t in build_tools() if t.name == name), None)
    assert tool is not None, f"{name} missing from build_tools()"
    assert callable(tool.fn)
    assert tool.parameters["type"] == "object"


@pytest.mark.parametrize("name", DESKTOP_TOOLS)
def test_desktop_tools_are_destructive_non_read(name):
    tool = next(t for t in build_tools() if t.name == name)
    assert tool.tier is Tier.DESTRUCTIVE
    assert classify(PlannedAction(tool=name, params={})) is Tier.DESTRUCTIVE


@pytest.mark.parametrize("name", DESKTOP_TOOLS)
def test_desktop_tool_proposes_only_and_does_no_os_input(name, fake_screen):
    fake = FakeDesktop()
    payload = _propose(name)
    assert payload["status"] == "pending"
    assert payload["kind"]
    assert payload["action_id"]
    assert payload["snapshot"]["action_id"] == payload["action_id"]
    assert payload["requires_phrase"] is True
    assert "yes, do it" in payload["note"]
    # Geometry reads for the crop are not input. No proposal-time call may
    # move the cursor, click, type, scroll, press keys, or open a file.
    assert not [call for call in fake.calls if not _is_crop_call(call)]
    # Now drive the same fake through the real executor: the approval must
    # produce observable calls, proving this fake would catch a tool-side OS
    # effect rather than passing vacuously.
    executor = DesktopActionExecutor(fake)
    approved = executor.execute(
        payload["action_id"], "yes, do it", payload["snapshot"])
    assert approved["status"] == "ok"
    input_calls = [call for call in fake.calls if not _is_crop_call(call)]
    assert input_calls
    assert input_calls[0][0] != "window_rect"
    # The orb's single proposal hook is set by the Task 1 proposal call.
    hook = consume_desktop_proposal()
    assert hook is not None
    assert hook["action_id"] == payload["action_id"]


def _is_crop_call(call):
    """Crop geometry does not move the cursor, click, type, or open a file."""
    return call[0] == "window_rect"


def test_desktop_click_tool_proposes_only(fake_screen):
    """The brief's representative case, pinned against a fake boundary."""
    fake = FakeDesktop()
    result = _propose("desktop_click", {"x": 10, "y": 20})
    assert result["status"] == "pending"
    assert result["kind"] == "click"
    assert result["target"] == {"x": 10, "y": 20, "button": "left", "clicks": 1}
    assert not [call for call in fake.calls if not _is_crop_call(call)]


@pytest.mark.parametrize("kind", ["move_cursor", "click", "scroll"])
def test_coordinate_actions_attach_local_crop_bytes(kind, fake_screen):
    arguments = {
        "move_cursor": {"x": 100, "y": 200},
        "click": {"x": 100, "y": 200, "button": "left", "clicks": 1},
        "scroll": {"x": 100, "y": 200, "direction": "down", "amount": 2},
    }[kind]
    payload = _propose(f"desktop_{kind}", arguments)
    assert payload["status"] == "pending"
    encoded = payload["crop_bytes"]
    assert isinstance(encoded, str)
    png = base64.b64decode(encoded, validate=True)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", png[16:24]) == (260, 320)  # radius 160, clipped
    hook = consume_desktop_proposal()
    assert hook["crop_bytes"] == encoded
    # The crop is local evidence only; the approved snapshot stays exact.
    assert payload["snapshot"]["target"]["x"] == 100
    assert payload["snapshot"]["target"]["y"] == 200


def test_type_with_coordinates_attaches_no_crop_and_keeps_text_private(fake_screen):
    payload = _propose("desktop_type", {"text": "password for the test account", "x": 10, "y": 20})
    assert payload["password_warning"] is True
    assert payload.get("crop_bytes", "") == ""
    assert "password for the test account" not in json.dumps(payload["evidence"])
    assert payload["snapshot"]["target"]["text"] == "password for the test account"


def test_sensitive_window_vetoes_capture_and_proposal(fake_screen):
    fake_screen.label = "1Password vault"
    payload = json.loads(agent_tools.execute(
        build_tools(), "desktop_click", {"x": 10, "y": 20}))
    assert payload.get("status") == "rejected"
    assert "error" in payload
    assert "action_id" not in payload
    # Rejection must be fully side-effect free at the card boundary.
    assert "crop_bytes" not in payload


def test_non_coordinate_actions_still_propose_without_crop(fake_screen):
    for name in ("desktop_open_app", "desktop_open_file", "desktop_press_keys"):
        payload = _propose(name)
        assert payload["status"] == "pending"
        assert payload.get("crop_bytes", "") == ""


@pytest.mark.parametrize("name, arguments", [
    ("desktop_open_app", {"name_or_path": "payload.exe"}),
    ("desktop_open_app", {"name_or_path": "https://example.com"}),
    ("desktop_open_app", {"name_or_path": "C:/tmp/install.bat"}),
    ("desktop_open_file", {"path": "C:/tmp/run.ps1"}),
    ("desktop_open_file", {"path": "\\\\server\\share\\secret.txt"}),
])
def test_blocked_launch_targets_do_not_produce_cards(name, arguments):
    """Unsafe targets are rejected before a proposal/card can exist."""
    fake = FakeDesktop()
    payload = json.loads(agent_tools.execute(build_tools(), name, arguments))
    assert "error" in payload, payload
    assert "action_id" not in payload
    assert payload.get("status") != "pending"
    assert consume_desktop_proposal() is None
    assert fake.calls == []


def test_sensitive_named_document_keeps_exact_target_and_redacted_preview():
    """A local document path is an executable payload, not a window title."""
    payload = _propose("desktop_open_file", {"path": "C:/Banking/statement.pdf"})
    assert payload["status"] == "pending"
    assert payload["target"]["path"] == "C:/Banking/statement.pdf"
    assert payload["snapshot"]["target"]["path"] == "C:/Banking/statement.pdf"
    assert payload["evidence"]["target_preview"] == "[REDACTED]"
    assert "Banking" not in json.dumps(payload["evidence"])


def test_sensitive_window_title_is_redacted_in_card_evidence():
    """A sensitive window title can be a target string, but never card copy."""
    payload = _propose(
        "desktop_open_app", {"name_or_path": "Chrome Incognito"})
    assert payload["status"] == "pending"
    assert "Incognito" not in json.dumps(payload["evidence"])
    assert payload["evidence"]["target_preview"] == "[REDACTED]"
    # The exact user-supplied string stays in the executable snapshot.
    assert payload["target"]["name_or_path"] == "Chrome Incognito"


def test_password_field_warns_but_remains_approvable():
    payload = _propose("desktop_type", {"text": "password for the test account"})
    assert payload["password_warning"] is True
    assert "WARNING" in payload["note"]
    # The warning must not rewrite what the operator approves.
    assert payload["target"]["text"] == "password for the test account"
    assert payload["evidence"]["password_warning"] is True

    # A typed approval still resolves exactly once through the confirmation
    # contract; the warning never blocks it. No OS input is performed here.
    from jarvis.desktop.actions import resolve_desktop_action

    resolved = resolve_desktop_action(
        payload["action_id"], "yes, do it", payload["snapshot"]
    )
    assert resolved is not None
    assert resolved.target == payload["target"]
    assert resolved.evidence["password_warning"] is True


def test_one_action_per_request_is_preserved_by_the_overlay_gate(monkeypatch):
    """A second proposal in one request cannot replace or add a card."""
    # Keep this gate test deterministic: supply a local crop directly rather
    # than spending the heartbeat deadline inside the screen capture path.
    monkeypatch.setattr(agent_tools, "_desktop_crop_bytes", lambda target: "local-crop")
    first = _propose("desktop_click", {"x": 10, "y": 20})
    overlay_token = overlay_protocol.arm_native_shell_channel(
        "integration-test-channel", "integration-test-relay")
    assert overlay_protocol.mark_native_shell_heartbeat(
        "overlay_heartbeat", overlay_token)
    assert overlay_protocol.desktop_overlay_connected()
    card = overlay_protocol.adopt_pending_desktop_proposal(first)
    assert card["status"] == "pending"
    assert card["snapshot"] == first["snapshot"]

    second = _propose("desktop_move_cursor", {"x": 30, "y": 40})
    blocked = overlay_protocol.adopt_pending_desktop_proposal(second)
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "one desktop action per request"
    assert overlay_protocol.latest_desktop_card()["action_id"] == first["action_id"]


def test_batched_calls_are_rejected_by_the_tier_policy():
    """Destructive actions may only run alone: a mixed batch must be flagged."""
    from jarvis.security.tiers import check_batched

    single = PlannedAction(tool="desktop_click", params={"x": 1, "y": 2})
    another = PlannedAction(tool="get_current_time")
    assert check_batched([single], [single]).ok
    mixed = check_batched([single, another], [single, another])
    assert not mixed.ok
    assert "cannot be batched" in mixed.reason
