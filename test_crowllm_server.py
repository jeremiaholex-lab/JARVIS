"""Regression tests for server-side CrowLLM voice selection."""
from __future__ import annotations

import asyncio

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


def test_voice_selection_uses_crowllm_when_configured(monkeypatch):
    """The explicit CrowLLM backend must choose the external voice."""
    monkeypatch.setitem(server.SETTINGS, "voice_backend", "crowllm")
    monkeypatch.setitem(server.ENV, "CROWLLM_API_KEY", "test-key")
    monkeypatch.setitem(server.ENV, "CROWLLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setitem(server.SETTINGS, "crowllm_tts", {
        "base_url": "https://example.test/v1", "model": "tts-bruce-en"})

    calls = []

    def fake_crowllm_tts(text, **kwargs):
        calls.append((text, kwargs))
        return b"RIFF-test"

    class FakeProviders:
        @staticmethod
        def crowllm_tts(text, **kwargs):
            return fake_crowllm_tts(text, **kwargs)

    monkeypatch.setattr(server, "_provider_module", lambda: FakeProviders)
    monkeypatch.setattr(server, "MODELS", type("M", (), {"tts": object()})())

    wav, seconds = server.synthesize("Hello there.")

    assert wav == b"RIFF-test"
    assert seconds >= 0
    assert len(calls) == 1
    assert calls[0][0] == "Hello there."
    assert calls[0][1]["key"] == "test-key"
    assert calls[0][1]["model"] == "tts-bruce-en"
    assert server.voice_status()["backend"] == "crowllm"
