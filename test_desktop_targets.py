"""Launch policy and local card evidence tests.

Nothing here launches a process, uploads pixels, or captures the real screen:
capture is injected through a fake ``DesktopControl`` and a fake screen source.
"""
from __future__ import annotations

import struct
import zlib

import pytest

import jarvis.desktop.evidence as evidence_module
from jarvis.desktop.evidence import capture_target_crop
from jarvis.desktop.targets import validate_launch_target


def _png(width: int, height: int, color_type: int = 2, interlace: int = 0) -> bytes:
    """Build a valid PNG with stdlib only, so no imaging dependency is needed."""
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    raw = b"".join(
        b"\x00" + (b"\x10\x20\x30\x40"[:channels] * width) for _ in range(height)
    )

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    head = (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type,
                                         0, 0, interlace)))
    if color_type == 3:
        head += chunk(b"PLTE",
                      b"".join(bytes([i * 16 % 256, i * 32 % 256, 0])
                               for i in range(256)))
    return (head
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _dimensions(png: bytes) -> tuple[int, int]:
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    return struct.unpack(">II", png[16:24])


def _ihdr(png: bytes) -> tuple[int, int, int]:
    """Bit depth, color type, and interlace of a PNG's IHDR chunk."""
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    depth, color_type, _comp, _filter, interlace = struct.unpack("BBBBB", png[24:29])
    return depth, color_type, interlace


class FakeControl:
    """Minimal DesktopControl stand-in: only window geometry is relevant."""

    def __init__(self, rect: tuple[int, int, int, int] = (0, 0, 1000, 800)) -> None:
        self.rect = rect
        self.focused: list[int] = []

    def window_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        return self.rect

    def focus_window(self, hwnd: int) -> None:
        self.focused.append(hwnd)


class FakeWindow:
    def __init__(self, label: str, hwnd: str = "1") -> None:
        self.hwnd = hwnd
        self.label = label


@pytest.fixture
def fake_capture(monkeypatch):
    """Replace the screen facility; no GDI call can escape this test."""
    calls: list[dict] = []

    class FakeScreenCapture:
        def __init__(self) -> None:
            calls.append({"init": True})

        def grab(self):
            screen = _png(1000, 800)
            calls.append({"grab": (1000, 800)})
            return evidence_module.Screen(png=screen, width=1000, height=800,
                                          captured_at=0.0, backend="fake")

        def list_windows(self):
            calls.append({"list_windows": True})
            return [FakeWindow(getattr(self, "window_label", "Chrome - Notes"))]

    monkeypatch.setattr(evidence_module, "ScreenCapture", FakeScreenCapture)
    return calls


@pytest.mark.parametrize("target", ["app.exe", "setup.bat", "run.ps1", "https://example.com"])
def test_unsafe_launch_targets_are_rejected(target):
    assert validate_launch_target(target)[0] is False


@pytest.mark.parametrize("target", ["Notes.txt", "Projects", "Chrome.lnk"])
def test_safe_launch_targets_are_allowed(target):
    assert validate_launch_target(target)[0] is True


@pytest.mark.parametrize("target", [
    "http://example.com", "file:///C:/Windows/notepad.exe", "C:\\temp\\payload.js",
    "run.vbs", "deploy.cmd", "install.msi", "C:/temp/setup.exe",
    "C:/temp/evil.EXE", "  ftp://host/file  ", "", "   ",
    # Finding 1: the "common application" Program Files exception was broader
    # than intended - any drive-qualified path containing the substring
    # escaped the .exe block. All .exe targets are blocked now, bare or
    # qualified, no exceptions.
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/evil/payload.exe",
    "C:\\Users\\HP\\Documents\\Program Files\\payload.exe",
])
def test_other_scripts_schemes_and_executables_are_rejected(target):
    allowed, reason = validate_launch_target(target)
    assert allowed is False
    assert reason  # the rejection must explain itself without exposing secrets


@pytest.mark.parametrize("target", [
    "C:/Users/HP/Documents/Notes.txt", "C:\\Users\\HP\\Desktop\\Projects",
    "D:/Work/Chrome.lnk",
])
def test_common_documents_folders_and_shortcuts_are_allowed(target):
    assert validate_launch_target(target)[0] is True


# Finding 3: drive-qualified paths bypassed the colon check, so Windows
# alternate data streams were accepted. The drive letter is the only ':'
# allowed anywhere in the path portion.
@pytest.mark.parametrize("target", [
    "C:/Users/HP/Documents/Notes.txt:hidden",
    "C:\\Users\\HP\\Documents\\Notes.txt:hidden",
    "C:/Program Files/app.lnk:stream",
])
def test_alternate_data_streams_on_drive_paths_are_rejected(target):
    allowed, reason = validate_launch_target(target)
    assert allowed is False
    assert reason


def test_local_paths_with_sensitive_names_are_allowed():
    """Sensitive names are redacted in card evidence, not in local targets."""
    for target in ("C:/Banking/statement.pdf", "C:/Banking/1Password vault.lnk"):
        allowed, _reason = validate_launch_target(target)
        assert allowed is True


def test_target_reasons_are_english_only():
    for target in ("app.exe", "https://example.com", "Notes.txt"):
        _allowed, reason = validate_launch_target(target)
        assert reason.isascii()
        assert reason


def test_crop_is_local_png_clipped_to_screen_bounds(fake_capture):
    control = FakeControl()
    png = capture_target_crop(control, 990, 790, radius=160)
    # The 320px box around (990, 790) extends past the 1000x800 screen; the
    # crop keeps only the pixels actually on screen (left edge 830, top 630).
    assert _dimensions(png) == (170, 170)
    assert fake_capture[-1] == {"grab": (1000, 800)}


def test_crop_is_square_around_target(fake_capture):
    control = FakeControl()
    assert _dimensions(capture_target_crop(control, 500, 400, radius=50)) == (100, 100)


def test_crop_refuses_invalid_coordinates_and_radius(fake_capture):
    control = FakeControl()
    for bad in ((-1, 10, 10), (10, -1, 10), (10, 10, 0), (10, 10, -5)):
        with pytest.raises(ValueError):
            capture_target_crop(control, *bad)
    assert fake_capture == []


def test_crop_uses_the_shared_redaction_policy(fake_capture, monkeypatch):
    control = FakeControl()
    seen = []

    def spy(value: str) -> str:
        seen.append(value)
        return "[REDACTED]"

    monkeypatch.setattr(evidence_module, "redact_sensitive", spy)
    # Any non-sensitive label becomes '[REDACTED]' under the spy, proving the
    # evidence module calls the shared redaction function rather than carrying
    # its own pattern list.
    with pytest.raises(evidence_module.SensitiveTargetError):
        capture_target_crop(control, 100, 100)
    assert seen == [" Chrome - Notes  "]
    assert not any("grab" in call for call in fake_capture)


def test_real_sensitive_window_vetoes_capture(monkeypatch):
    class SensitiveCapture:
        def list_windows(self):
            return [FakeWindow("1Password vault")]

        def grab(self):
            raise AssertionError("no pixel may be read for a sensitive window")

    monkeypatch.setattr(evidence_module, "ScreenCapture", SensitiveCapture)
    with pytest.raises(evidence_module.SensitiveTargetError):
        capture_target_crop(FakeControl(), 100, 100)


def test_crop_fails_closed_when_screen_source_is_unavailable(monkeypatch):
    class BrokenCapture:
        def list_windows(self):
            return []

        def grab(self):
            raise evidence_module.ScreenCaptureError("no screen backend")

    monkeypatch.setattr(evidence_module, "ScreenCapture", BrokenCapture)
    with pytest.raises(evidence_module.ScreenCaptureError):
        capture_target_crop(FakeControl(), 100, 100)


# Finding 2: the GDI capture path emits 8-bit RGBA PNGs (color type 6); the
# decoder accepted only 8-bit RGB (color type 2), so real captures could
# never produce a card crop.
@pytest.mark.parametrize("color_type", [6, 2, 3])
def test_crop_accepts_gdi_rgba_and_palette_sources(monkeypatch, color_type):
    class RgbaCapture:
        def list_windows(self):
            return [FakeWindow("Chrome - Notes")]

        def grab(self):
            screen = _png(1000, 800, color_type=color_type)
            return evidence_module.Screen(png=screen, width=1000, height=800,
                                          captured_at=0.0, backend="fake")

    monkeypatch.setattr(evidence_module, "ScreenCapture", RgbaCapture)
    png = capture_target_crop(FakeControl(), 500, 400, radius=50)
    assert _dimensions(png) == (100, 100)
    # The card crop is always a compact RGB image, whatever the source.
    assert _ihdr(png) == (8, 2, 0)


def test_crop_rejects_interlaced_sources(monkeypatch):
    class InterlacedCapture:
        def list_windows(self):
            return [FakeWindow("Chrome - Notes")]

        def grab(self):
            screen = _png(100, 80, color_type=6, interlace=1)
            return evidence_module.Screen(png=screen, width=100, height=80,
                                          captured_at=0.0, backend="fake")

    monkeypatch.setattr(evidence_module, "ScreenCapture", InterlacedCapture)
    with pytest.raises(evidence_module.ScreenCaptureError):
        capture_target_crop(FakeControl(), 10, 10, radius=10)
