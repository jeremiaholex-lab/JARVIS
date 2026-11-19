"""Vision path tests. Nothing here sends real pixels to a cloud endpoint — the
consent gate is what is under test."""
import base64
import struct
import zlib

import pytest

from jarvis.vision.describe import CONSENT_NEEDED, consent_given, describe_screen, local_vision_model, looks_like_screen_request
from jarvis.vision.screen import Screen, ScreenCapture, ScreenCaptureError, _png_dimensions


def _png(width=4, height=3) -> bytes:
    """A tiny valid PNG, built with stdlib only."""
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _screen() -> Screen:
    png = _png()
    return Screen(png=png, width=4, height=3, captured_at=0.0, backend="test")


class FakeEngine:
    def __init__(self, models, response=None, raises=False):
        self.models = models
        self.response = response or {"content": "The error says FileNotFoundError on line 12."}
        self.raises = raises
        self.calls = []

    def list_models(self):
        return self.models

    def generate(self, msgs, **kwargs):
        self.calls.append((msgs, kwargs))
        if self.raises:
            raise RuntimeError("vision model crashed")
        return self.response


class ExplodingCloud:
    """Any attempt to reach the cloud in these tests must fail the test."""

    def __call__(self):
        raise AssertionError("cloud vision was contacted without an explicit consent test")


def test_no_local_vision_model_means_no_cloud_call_without_consent():
    engine = FakeEngine(["qwen3:1.7b", "qwen3:8b", "deepseek-r1:1.5b"])
    result = describe_screen(_screen(), "what error is on my screen",
                             local_engine=engine, cloud_factory=ExplodingCloud(), settings={})
    assert result.where == "none"
    assert result.text == CONSENT_NEEDED
    assert "consent" in result.reason


def test_local_vision_model_is_used_free_of_consent():
    engine = FakeEngine(["qwen3:1.7b", "llava:13b"])
    result = describe_screen(_screen(), "what is the error",
                             local_engine=engine, cloud_factory=ExplodingCloud(), settings={})
    assert result.where == "local"
    assert result.model == "llava:13b"
    assert "FileNotFoundError" in result.text
    # the image actually travelled inside the local message, as base64
    sent = engine.calls[0][0][0].images[0]
    assert base64.b64decode(sent) == _png()


def test_local_vision_failure_still_refuses_cloud_without_consent():
    engine = FakeEngine(["llava:13b"], raises=True)
    result = describe_screen(_screen(), "read this", local_engine=engine,
                             cloud_factory=ExplodingCloud(), settings={})
    assert result.where == "none"
    assert result.text == CONSENT_NEEDED


def test_consent_can_be_granted_by_setting_or_env(tmp_path, monkeypatch):
    assert consent_given({}) is False
    assert consent_given({"vision": {"cloud_consent": True}}) is True
    monkeypatch.setenv("JARVIS_VISION_CONSENT", "1")
    assert consent_given({}) is True
    monkeypatch.setenv("JARVIS_VISION_CONSENT", "0")
    assert consent_given({}) is False


def test_screen_requests_are_recognized():
    assert looks_like_screen_request("look at this error on my screen")
    assert looks_like_screen_request("what can you see right now")
    assert not looks_like_screen_request("what is my capital of france")


def test_capture_produces_a_real_png_on_this_machine():
    """Skips where there is no display (CI); the desktop box has one."""
    if not ScreenCapture().available():
        pytest.skip("no screen capture backend on this host")
    screen = ScreenCapture().grab()
    assert screen.png.startswith(b"\x89PNG")
    assert (screen.width, screen.height) == _png_dimensions(screen.png)
    assert screen.width >= 640


def test_second_grab_inside_ttl_is_cached():
    import time as _time
    grabs = {"n": 0}

    def fake_grab(self):
        grabs["n"] += 1
        png = _png()
        return Screen(png=png, width=4, height=3, captured_at=_time.time(), backend="test")

    original = ScreenCapture._grab
    ScreenCapture._grab = fake_grab
    try:
        cap = ScreenCapture(ttl=30)
        first = cap.grab()
        second = cap.grab()
        assert first is second
        assert grabs["n"] == 1
    finally:
        ScreenCapture._grab = original


def test_implausible_capture_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr("jarvis.vision.screen._capture_windows", lambda p: "fake")
    monkeypatch.setattr(Path := __import__("pathlib", fromlist=["Path"]).Path, "read_bytes",
                        lambda self: b"not a png")
    with pytest.raises(ScreenCaptureError):
        ScreenCapture()._grab()
