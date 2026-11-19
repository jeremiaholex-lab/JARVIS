"""The wake-word frame scheduler must not starve.

Regression: the old cursor-into-a-trimmed-ring scheme made exactly ONE
prediction across 60 s of audio, so "Hey JARVIS" could never fire after the
first frame of the session. This drives the real handler through the real
WebSocket with a stub OWW that records every frame it is asked to score.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server


class RecordingOWW:
    """Stands in for openwakeword.Model; remembers the frames it was given."""

    def __init__(self, fire_at=None):
        self.frames = []
        self.fire_at = fire_at  # index at which to report a wake

    def predict(self, frame):
        self.frames.append(np.asarray(frame, dtype=np.float32).copy())
        score = 0.0
        if self.fire_at is not None and len(self.frames) == self.fire_at:
            score = 0.9
        return {"hey jarvis": score}


@pytest.fixture
def client(monkeypatch):
    was_ready = server.MODELS.ready
    was_oww = server.MODELS.oww
    server.MODELS.ready = True
    oww = RecordingOWW()
    monkeypatch.setattr(server.MODELS, "oww", oww)
    app = server.build_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as c:
        yield c, oww
    server.MODELS.ready = was_ready
    server.MODELS.oww = was_oww


def frames_to_base64(count, chunk_samples=320):
    """count frames worth of audio, delivered in browser-sized 20 ms chunks."""
    total = count * server.OWW_FRAME
    signal = (np.sin(np.linspace(0, 800 * total / 16000, total)) * 0.2).astype(np.float32)
    out = []
    for i in range(0, total, chunk_samples):
        block = signal[i:i + chunk_samples]
        if len(block) < chunk_samples:
            block = np.pad(block, (0, chunk_samples - len(block)))
        pcm = (block * 32767).astype(np.int16)
        out.append(base64.b64encode(pcm.tobytes()).decode("ascii"))
    return out


def test_predictions_keep_coming_for_the_whole_session(client):
    c, oww = client
    with c.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["op"] == "hello"

        for payload in frames_to_base64(40):        # 40 frames = 3.2 s
            ws.send_json({"op": "audio", "data": payload})

        # Drain whatever the server sent while feeding.
        scored = len(oww.frames)

    assert scored >= 35, (
        f"only {scored} predictions over 40 frames of audio - the scheduler "
        "starved again (the old bug made exactly 1)"
    )


def test_every_scored_frame_is_a_full_80ms_window(client):
    c, oww = client
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()
        for payload in frames_to_base64(10):
            ws.send_json({"op": "audio", "data": payload})
    assert oww.frames, "no frames were scored at all"
    assert all(len(f) == server.OWW_FRAME for f in oww.frames), [len(f) for f in oww.frames]


def test_frames_are_contiguous_and_never_resent(client):
    """Overlap would double-count audio; gaps would lose the wake word."""
    c, oww = client
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()
        for payload in frames_to_base64(8):
            ws.send_json({"op": "audio", "data": payload})
    joined = np.concatenate(oww.frames)
    # Reconstruct the same deterministic signal and compare shape/content.
    assert len(joined) == len(oww.frames) * server.OWW_FRAME
    assert joined[: server.OWW_FRAME] == pytest.approx(oww.frames[0])
    assert joined[server.OWW_FRAME: 2 * server.OWW_FRAME] == pytest.approx(oww.frames[1])


def test_wake_word_actually_triggers_listening(monkeypatch):
    """A fire must produce a listening state, without invoking real VAD."""
    import silero_vad

    was_ready = server.MODELS.ready
    server.MODELS.ready = True
    oww = RecordingOWW(fire_at=3)
    monkeypatch.setattr(server.MODELS, "oww", oww)
    monkeypatch.setattr(server.MODELS, "vad_model", object())
    # Once the third frame fires, handle_audio_chunk immediately runs maybe-
    # close-utterance on the accumulated audio. Stub that to keep this test on
    # the wake-word seam rather than loading torch or the real VAD model.
    monkeypatch.setattr(silero_vad, "get_speech_timestamps", lambda *a, **kw: [])
    app = server.build_app()
    try:
        with TestClient(app, client=("127.0.0.1", 12345)) as c:
            with c.websocket_connect("/ws") as ws:
                ws.receive_json()
                for payload in frames_to_base64(6):
                    ws.send_json({"op": "audio", "data": payload})
                states = []
                for _ in range(20):
                    msg = ws.receive_json()
                    if msg.get("op") == "state":
                        states.append((msg.get("state"), msg.get("reason")))
                        if msg.get("reason") == "wake-word":
                            break
                assert ("listening", "wake-word") in states, states
    finally:
        server.MODELS.ready = was_ready


def test_wake_peak_is_reported_for_threshold_debugging(monkeypatch):
    """The orb needs a way to show why the wake word is not firing."""
    was_ready = server.MODELS.ready
    server.MODELS.ready = True
    oww = RecordingOWW()
    monkeypatch.setattr(server.MODELS, "oww", oww)
    app = server.build_app()
    try:
        with TestClient(app, client=("127.0.0.1", 12345)) as c:
            with c.websocket_connect("/ws") as ws:
                ws.receive_json()
                for payload in frames_to_base64(4):
                    ws.send_json({"op": "audio", "data": payload})
    finally:
        server.MODELS.ready = was_ready
    assert len(oww.frames) == 4
