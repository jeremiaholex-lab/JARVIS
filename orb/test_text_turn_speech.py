"""Regression tests for the text turn's speech event contract."""
from __future__ import annotations

import asyncio

import pytest

from orb import server


class _Queue:
    """Minimal asyncio queue stand-in: records emitted events synchronously."""

    def __init__(self) -> None:
        self.events = []

    def put_nowait(self, event):
        self.events.append(event)

    async def get(self):
        raise AssertionError("the test stops before playback")


class _Client:
    def __init__(self) -> None:
        self.queue = _Queue()
        self.history = []
        self.pending_learning = None
        self.playback_id = None
        self.playback_active = False
        self.barge_frames = 99
        self.barge_candidate = [object()]
        self.barge_audio = [object()]
        self.barge_audio_samples = 99
        self.thinking = True
        self.turn_task = None

    def emit(self, q, op, **payload):
        q.put_nowait({"op": op, **payload})

    def remember(self, role, text, *args):
        self.history.append({"role": role, "text": text})


class _FakeLatch:
    def set(self):
        pass

    def clear(self):
        pass


class _FakeUserModel:
    def prompt_block(self):
        return ""

    def remember_explicit(self, text):
        return None


class _FakeTier:
    id = "gemini35"
    label = "Gemini 3.5 Flash"
    available = True


class _FakeLadder:
    def primary_label(self):
        return "Gemini 3.5 Flash"


class _FakeAgentContext:
    user_model = _FakeUserModel()
    semantic = None
    schemas = []
    tools = {}
    ladder = _FakeLadder()
    cloud_available = False
    local_tok_s = None

    def call_engine(self, engine, messages, tools, max_tokens=400):
        return {"content": "Ready", "tool_calls": []}

    def pending_writes(self):
        return []

    def pending_edits(self):
        return []


class _FakeTTS:
    def synthesize(self, text, **kwargs):
        raise AssertionError("the test stops before synthesis")


def test_typed_turn_labels_the_selected_hosted_voice(monkeypatch):
    """tts_start must name the hosted backend actually selected for the turn."""
    monkeypatch.setattr(server, "agent_context", lambda: _FakeAgentContext())
    monkeypatch.setattr(server, "MODELS", type("M", (), {"tts": _FakeTTS()})())
    monkeypatch.setattr(server, "_speech_begin", lambda: None)
    monkeypatch.setattr(server, "SPEECH_ACTIVE", _FakeLatch())
    monkeypatch.setattr(server, "_voice_is_hosted", lambda: True)
    monkeypatch.setitem(server.SETTINGS, "voice_backend", "crowllm")
    monkeypatch.setitem(server.SETTINGS, "voice_pacing", "quick")
    monkeypatch.setitem(server.SETTINGS, "speak_max_chars", 0)

    async def run_turn_only(func, *args, **kwargs):
        if func.__name__ == "run_turn":
            from types import SimpleNamespace
            return SimpleNamespace(answer="Ready", engine="test",
                                   route_reason="test", steps=[], tools_used=[])
        raise AssertionError("the test stops before synthesis")

    monkeypatch.setattr(server.asyncio, "to_thread", run_turn_only)

    client = _Client()
    app = server.build_app()
    ws_route = next((r for r in app.routes if getattr(r, "path", "") == "/ws"), None)
    ws_handler = getattr(ws_route, "endpoint", None)
    session = None
    for cell in getattr(ws_handler, "__closure__", None) or ():
        try:
            candidate = cell.cell_contents
        except ValueError:
            continue
        if callable(candidate) and getattr(candidate, "__name__", "") == "_session":
            session = candidate
            break
    freevars = getattr(getattr(session, "__code__", None), "co_freevars", ())
    for name, cell in zip(freevars, getattr(session, "__closure__", None) or ()):
        if name == "do_turn":
            do_turn = cell.cell_contents
            break
    else:
        pytest.skip("do_turn closure is unavailable in this server build")

    try:
        asyncio.run(do_turn(client, client.queue, "hello", [], force_cloud=False))
    except AssertionError as exc:
        assert "stops before synthesis" in str(exc)

    starts = [e for e in client.queue.events if e.get("op") == "tts_start"]
    assert len(starts) == 1
    assert starts[0]["backend"] == "crowllm"


def test_typed_turn_emits_tts_start_with_a_backend(monkeypatch):
    """A typed reply must announce which voice will speak it.

    The event is emitted before synthesis begins, so the browser can start
    local playback immediately and pre-roll only hosted audio. Computing the
    backend from a variable assigned later in the function made every typed
    turn fail at this event.
    """
    monkeypatch.setattr(server, "agent_context", lambda: _FakeAgentContext())
    monkeypatch.setattr(server, "MODELS", type("M", (), {"tts": _FakeTTS()})())
    monkeypatch.setattr(server, "_speech_begin", lambda: None)
    monkeypatch.setattr(server, "SPEECH_ACTIVE", _FakeLatch())
    monkeypatch.setattr(server, "_voice_is_hosted", lambda: False)
    monkeypatch.setitem(server.SETTINGS, "voice_pacing", "smooth")
    monkeypatch.setitem(server.SETTINGS, "speak_max_chars", 0)

    async def run_turn_only(func, *args, **kwargs):
        if func.__name__ == "run_turn":
            from types import SimpleNamespace
            return SimpleNamespace(answer="Ready", engine="test",
                                   route_reason="test", steps=[], tools_used=[])
        raise AssertionError("the test stops before synthesis")

    monkeypatch.setattr(server.asyncio, "to_thread", run_turn_only)

    client = _Client()
    app = server.build_app()
    # do_turn is a closure in build_app. Expose it for this regression test;
    # production behavior is identical because it is the same callable the
    # websocket handler uses.
    do_turn = None
    ws_route = next((r for r in app.routes if getattr(r, "path", "") == "/ws"), None)
    ws_handler = getattr(ws_route, "endpoint", None)
    session = None
    for cell in getattr(ws_handler, "__closure__", None) or ():
        try:
            candidate = cell.cell_contents
        except ValueError:
            continue
        if callable(candidate) and getattr(candidate, "__name__", "") == "_session":
            session = candidate
            break
    freevars = getattr(getattr(session, "__code__", None), "co_freevars", ())
    for name, cell in zip(freevars, getattr(session, "__closure__", None) or ()):
        if name == "do_turn":
            do_turn = cell.cell_contents
            break
    if do_turn is None:
        pytest.skip("do_turn closure is unavailable in this server build")

    async def _run():
        await do_turn(client, client.queue, "hello", [], force_cloud=False)

    try:
        asyncio.run(_run())
    except AssertionError as exc:
        # to_thread is patched so the test stops when speech synthesis starts;
        # the assertion itself proves execution reached that stage.
        assert "stops before synthesis" in str(exc)

    starts = [e for e in client.queue.events if e.get("op") == "tts_start"]
    assert len(starts) == 1
    assert starts[0]["backend"] == "local"
