"""Boot ordering must not stall the orb.

Regression this file exists for: warming Kokoro before declaring readiness
looked reasonable (first synthesis costs 17-24s), but on this 15.8 GB laptop
the wait never finished once llama-server's 16k-context KV cache was resident -
the backend bound the port, reported models_ready=false indefinitely, and the
orb was unusable until restart. Readiness must depend on the models only.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import server


class FakeTTS:
    def __init__(self, delay: float):
        self.delay = delay
        self.calls = 0

    def synthesize(self, text, voice_id="", speed=1.0):
        self.calls += 1
        time.sleep(self.delay)
        audio = b"RIFF" + bytes(4) + b"WAVE"
        return type("R", (), {"audio": audio})()


def _patch(monkeypatch, tts_delay: float):
    """Stub the heavy model loads so only the boot ordering is under test."""
    monkeypatch.setattr(server.MODELS, "ready", False)
    monkeypatch.setattr(server.MODELS, "tts_ready", False)
    monkeypatch.setattr(server.MODELS, "stage", "not started")

    import openwakeword  # noqa: F401  (module already imported by server)

    class FakeOWW:
        def __init__(self, **kwargs):
            pass

    class FakeWhisper:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr("openwakeword.Model", FakeOWW, raising=False)
    monkeypatch.setattr("faster_whisper.WhisperModel", FakeWhisper, raising=False)
    monkeypatch.setattr("silero_vad.load_silero_vad", lambda: object(), raising=False)
    monkeypatch.setattr(
        "openjarvis.speech.kokoro_tts.KokoroTTSBackend",
        lambda *a, **k: FakeTTS(tts_delay),
        raising=False,
    )


def test_readiness_does_not_wait_for_the_voice_warmup(monkeypatch):
    """A slow voice init must not hold up 'ready'."""
    _patch(monkeypatch, tts_delay=6.0)

    started = time.monotonic()
    server.MODELS.load()
    elapsed = time.monotonic() - started

    assert server.MODELS.ready is True
    assert elapsed < 3.0, f"load() blocked for {elapsed:.1f}s waiting on TTS"
    assert server.MODELS.stage == "ready"


def test_warmup_still_completes_in_the_background(monkeypatch):
    _patch(monkeypatch, tts_delay=0.4)
    server.MODELS.load()
    assert server.MODELS.wait_tts(timeout=10.0) is True
    assert server.MODELS.tts_ready is True
    assert server.MODELS.warmup_thread is not None


def test_wait_tts_gives_up_rather_than_blocking_forever(monkeypatch):
    """The bounded wait protects a reply from a wedged warmup thread."""
    _patch(monkeypatch, tts_delay=30.0)
    server.MODELS.load()
    started = time.monotonic()
    ready = server.MODELS.wait_tts(timeout=1.0)
    waited = time.monotonic() - started
    assert ready is False
    assert waited < 2.5, f"wait_tts ignored its timeout ({waited:.1f}s)"


def test_status_endpoint_reports_boot_stage_without_logs(monkeypatch):
    """The shell buffers the child's stderr, so /api/status is the boot diary."""
    from fastapi.testclient import TestClient

    _patch(monkeypatch, tts_delay=0.0)
    server.MODELS.ready = False
    server.MODELS.stage = "speech-recognition"
    app = server.build_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        data = client.get("/api/status").json()
    assert data["stage"] == "speech-recognition"
    assert data["models_ready"] is False
    assert "voice_ready" in data


def test_local_context_window_is_capped_for_ram_reasons():
    """num_ctx=16384 reserved ~3.1 GB of KV cache here; the orb caps it."""
    defaults = server.default_settings()
    assert defaults["local_num_ctx"] == 4096
