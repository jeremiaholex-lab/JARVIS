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


# ---------------------------------------------------------------------------
# per-window perception: listing, targeting, and the tool surface
# ---------------------------------------------------------------------------

def _wline(hwnd: int, pid: int, proc: str, cls: str, title: str) -> str:
    return (f"{hwnd}|{pid}|{proc}|{cls}|"
            + base64.b64encode(title.encode("utf-8")).decode("ascii"))


def test_list_windows_parses_and_decodes_titles(monkeypatch):
    out = "\n".join([
        _wline(1234, 40, "msedge", "Chrome_WidgetWin_1",
               "Phrasly AI \u200b- Personal - Microsoft\u00ae Edge"),
        _wline(5678, 70, "explorer", "Progman", "Program Manager"),
        "garbage line without enough fields",
    ])
    monkeypatch.setattr(ScreenCapture, "_run_ps", staticmethod(lambda s: out))
    wins = ScreenCapture().list_windows()
    assert [w.hwnd for w in wins] == ["1234", "5678"]
    assert "\u00ae" in wins[0].title          # title survives UTF-8/base64
    assert "\u200b" in wins[0].title
    assert wins[1].process == "explorer"


def test_capture_window_targets_unique_match(monkeypatch):
    png = _png(8, 6)

    def fake_run_ps(script: str) -> str:
        # The script assigns $Out before Add-Type; write the fixture there.
        import re
        m = re.search(r'\$Out = "([^"]+)"', script)
        with open(m.group(1), "wb") as f:
            f.write(png)
        return "8x6\n"

    monkeypatch.setattr(ScreenCapture, "_run_ps", staticmethod(fake_run_ps))
    from jarvis.vision.screen import WindowInfo
    monkeypatch.setattr(ScreenCapture, "list_windows", lambda self: [
        WindowInfo(hwnd="1234", pid=40, process="msedge",
                   class_name="Chrome_WidgetWin_1",
                   title="Instagram - Microsoft Edge")])
    screen = ScreenCapture().capture_window("Instagram")
    assert (screen.width, screen.height) == (8, 6)
    assert screen.backend == "windows-printwindow"


def test_capture_window_ambiguous_lists_candidates(monkeypatch):
    from jarvis.vision.screen import WindowInfo
    monkeypatch.setattr(ScreenCapture, "list_windows", lambda self: [
        WindowInfo(hwnd="1", pid=1, process="a", class_name="x", title="Settings"),
        WindowInfo(hwnd="2", pid=2, process="b", class_name="y", title="Settings app"),
    ])
    with pytest.raises(ScreenCaptureError) as e:
        ScreenCapture().capture_window("Settings")
    assert "matches 2 windows" in str(e.value)


def test_capture_window_minimized_says_so(monkeypatch):
    from jarvis.vision.screen import WindowInfo
    monkeypatch.setattr(ScreenCapture, "list_windows", lambda self: [
        WindowInfo(hwnd="1", pid=1, process="code", class_name="x", title="Code Editor")])

    def fake_run_ps(script: str) -> str:
        return "MINIMIZED\n"
    monkeypatch.setattr(ScreenCapture, "_run_ps", staticmethod(fake_run_ps))
    with pytest.raises(ScreenCaptureError) as e:
        ScreenCapture().capture_window("Code")
    assert "minimized" in str(e.value)


def test_capture_window_unknown_lists_open_ones(monkeypatch):
    from jarvis.vision.screen import WindowInfo
    monkeypatch.setattr(ScreenCapture, "list_windows", lambda self: [
        WindowInfo(hwnd="1", pid=1, process="a", class_name="x", title="Chrome Browser")])
    with pytest.raises(ScreenCaptureError) as e:
        ScreenCapture().capture_window("Spotify")
    assert "Chrome Browser" in str(e.value)


def test_look_at_screen_tool_passes_window_target(monkeypatch):
    """The tool routes window=… to capture_window and keeps consent semantics."""
    import jarvis.agent.tools as agent_tools
    from jarvis.vision.screen import Screen
    captured: dict = {}

    def fake_capture_window(self, query):
        captured["query"] = query
        return Screen(png=_png(), width=8, height=6, captured_at=0.0,
                      backend="windows-printwindow")

    seen: dict = {}

    def fake_describe(screen, question, **kw):
        seen["question"] = question
        from jarvis.vision.describe import VisionResult
        return VisionResult("a cat video", "", "none", 5, reason="no consent")

    monkeypatch.setattr(ScreenCapture, "capture_window", fake_capture_window)
    monkeypatch.setattr(ScreenCapture, "available", staticmethod(lambda: True))
    monkeypatch.setattr("jarvis.vision.describe.describe_screen", fake_describe)
    agent_tools.configure_vision(lambda: (None, None, {}))
    import json as _json
    result = _json.loads(agent_tools._look_at_screen("what is playing", window="Instagram"))
    assert captured["query"] == "Instagram"
    assert result["seen"] == "a cat video"
    assert result["source"] == "windows-printwindow"


def test_list_open_windows_tool_shape(monkeypatch):
    import json as _json
    import jarvis.agent.tools as agent_tools
    from jarvis.vision.screen import WindowInfo
    monkeypatch.setattr(ScreenCapture, "list_windows", lambda self: [
        WindowInfo(hwnd="1", pid=1, process="msedge", class_name="x", title="Edge"),
        WindowInfo(hwnd="2", pid=2, process="explorer", class_name="y", title="Program Manager"),
    ])
    out = _json.loads(agent_tools._list_open_windows())
    assert {w["process"] for w in out["windows"]} == {"msedge", "explorer"}


def test_screen_tool_names_are_exposed_as_read(monkeypatch):
    import jarvis.agent.tools as agent_tools
    monkeypatch.setattr(ScreenCapture, "list_windows", lambda self: [])
    names = {t.name for t in agent_tools.build_tools()}
    assert {"look_at_screen", "list_open_windows"} <= names


def test_cloud_reads_reach_http_with_provider_key(monkeypatch):
    """Consent + a key carried by the host's cloud engine must produce a
    real HTTP call — the orb does not export AGNES_API_KEY to os.environ."""
    calls = {}

    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": "a terminal open on main.py"}}]}

    class Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None, headers=None):
            calls["url"] = url; calls["headers"] = headers
            return Resp()

    monkeypatch.setattr("jarvis.vision.describe.httpx.Client", Client)
    monkeypatch.delenv("AGNES_API_KEY", raising=False)
    settings = {"cloud": {"base_url": "https://api.test/v1", "model": "vision-x"},
                "vision": {"cloud_consent": True}}
    engine = FakeEngine(["qwen3:1.7b"])          # text-only: no local vision
    cloud = type("CE", (), {"_api_key": "sk-from-provider"})()
    result = describe_screen(_screen(), "what is open?", local_engine=engine,
                             cloud_factory=cloud, settings=settings,
                             api_key="sk-from-provider")
    assert result.where == "cloud"
    assert result.text == "a terminal open on main.py"
    assert calls["headers"]["Authorization"] == "Bearer sk-from-provider"


def test_tool_passes_engine_key_to_describe(monkeypatch):
    """_look_at_screen must hand describe_screen the key the engine carries."""
    import jarvis.agent.tools as agent_tools
    seen = {}

    def fake_describe(screen, question, **kw):
        seen.update(kw)
        from jarvis.vision.describe import VisionResult
        return VisionResult("ok", "m", "cloud", 1)

    monkeypatch.setattr("jarvis.vision.screen.ScreenCapture.available",
                        staticmethod(lambda: True))
    monkeypatch.setattr("jarvis.vision.screen.ScreenCapture.grab",
                        lambda self: _screen())
    monkeypatch.setattr("jarvis.vision.describe.describe_screen", fake_describe)
    cloud = type("CE", (), {"_api_key": "sk-123"})()
    agent_tools.configure_vision(lambda: (FakeEngine([]), cloud, {}))
    agent_tools._look_at_screen("what is this?")
    assert seen.get("api_key") == "sk-123"
