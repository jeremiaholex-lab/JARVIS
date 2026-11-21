"""Regression test for long-reply speech continuity."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from orb import server


class _Queue:
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

    def emit_bin(self, q, payload):
        q.put_nowait({"op": "binary", "bytes": len(payload)})

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


class _FakeLadder:
    def primary_label(self):
        return "Test"


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


def _extract_do_turn():
    app = server.build_app()
    route = next((r for r in app.routes if getattr(r, "path", "") == "/ws"), None)
    handler = getattr(route, "endpoint", None)
    for cell in getattr(handler, "__closure__", None) or ():
        try:
            candidate = cell.cell_contents
        except ValueError:
            continue
        if callable(candidate) and getattr(candidate, "__name__", "") == "_session":
            for name, free in zip(getattr(candidate, "__code__", None).co_freevars,
                                  getattr(candidate, "__closure__", None) or ()):
                if name == "do_turn":
                    return free.cell_contents
    raise AssertionError("do_turn closure is unavailable")


def test_local_long_reply_uses_one_gapless_synthesis(monkeypatch):
    """Quick local voice must not chunk a long reply into gappy pieces."""
    monkeypatch.setattr(server, "agent_context", lambda: _FakeAgentContext())
    monkeypatch.setattr(server, "MODELS", type("M", (), {"tts": object()})())
    monkeypatch.setattr(server, "_speech_begin", lambda: None)
    monkeypatch.setattr(server, "SPEECH_ACTIVE", _FakeLatch())
    monkeypatch.setattr(server, "_voice_is_hosted", lambda: False)
    monkeypatch.setitem(server.SETTINGS, "voice_pacing", "quick")
    monkeypatch.setitem(server.SETTINGS, "speak_max_chars", 0)

    answer = " ".join(f"Sentence {i} has useful detail." for i in range(8))
    started = []

    def fake_synthesize(text):
        started.append(text)
        return b"WAV", 0.01

    monkeypatch.setattr(server, "synthesize", fake_synthesize)

    async def to_thread_only(func, *args, **kwargs):
        if getattr(func, "__name__", "") == "run_turn":
            return SimpleNamespace(answer=answer, engine="test",
                                   route_reason="test", steps=[], tools_used=[])
        return func(*args, **kwargs)

    monkeypatch.setattr(server.asyncio, "to_thread", to_thread_only)
    monkeypatch.setattr(server.asyncio, "create_task", lambda coro: coro)

    do_turn = _extract_do_turn()
    client = _Client()
    asyncio.run(do_turn(client, client.queue, "hello", [], force_cloud=False))

    flushes = [e for e in client.queue.events if e.get("op") == "tts_flush"]
    assert len(started) == 1, "local voice should synthesize the reply once"
    assert len(flushes) == 1, "one gapless audio stream should reach the browser"
