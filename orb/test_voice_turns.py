"""Voice-first turns (roadmap #8): follow-up window + spoken confirmations.

Two seams are tested:

* the offline resolver, as pure logic — the shared confirmation grammar
  (jarvis.security.file_edit) against a pending set, with the whitespace
  compaction that STT output needs;
* the real WebSocket handler, driven the way test_wake_scheduler does it:
  a recording OWW stub, a stubbed VAD, a stubbed whisper, and a fake agent
  context that records every settle call. The model is never consulted on
  the confirmation paths — that is the property under test.
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server

PW = "pw_ab12cd34ef56ab78"
FE = "fe_cd34ab12ef56ab78"


def w(token: str = PW) -> dict:
    return {"token": token, "action": "jarvis_projects_write"}


def e(token: str = FE) -> dict:
    return {"token": token, "action": "jarvis_file_edit"}


# ---------------------------------------------------------------------------
# the offline resolver (pure)
# ---------------------------------------------------------------------------

def test_normalize_reglues_stt_spaced_tokens():
    # Whisper renders hex tokens with spaces: "pw ab12cd34ef56ab78".
    assert server.normalize_spoken_confirmation(
        "yes pw ab12cd34ef56ab78") == f"yes {PW}"


def test_normalize_compacts_whitespace():
    assert server.normalize_spoken_confirmation(
        "  no   fe_cd34ab12ef56ab78\t") == f"no {FE}"


def test_resolve_spoken_write_yes():
    v = server.resolve_voice_confirmation(f"yes {PW}", [w()], [e()])
    assert v == {"kind": "write", "token": PW, "approved": True}


def test_resolve_spoken_write_no_and_cancel():
    assert server.resolve_voice_confirmation(f"no {PW}", [w()], [])["approved"] is False
    assert server.resolve_voice_confirmation(f"cancel {PW}", [w()], [])["approved"] is False


def test_resolve_spoken_edit_confirm_carries_the_phrase():
    v = server.resolve_voice_confirmation(f"yes, do it {FE}", [], [e()])
    assert v["kind"] == "edit"
    assert v["approved"] is True
    assert v["phrase"] == "yes, do it"
    assert v["token"] == FE


def test_resolve_spoken_edit_cancel():
    v = server.resolve_voice_confirmation(f"no {FE}", [], [e()])
    assert v["kind"] == "edit" and v["approved"] is False


def test_bare_yes_never_applies_a_destructive_edit():
    # "yes fe_x" alone is not enough: an offline note explains the phrase,
    # exactly like the typed chat path.
    v = server.resolve_voice_confirmation(f"yes {FE}", [], [e()])
    assert v["kind"] == "note" and "destructive" in v["note"]


def test_phrase_aimed_at_a_write_token_is_a_note():
    v = server.resolve_voice_confirmation(f"yes, do it {PW}", [w()], [])
    assert v["kind"] == "note" and "pending write" in v["note"]


def test_bare_yes_picks_the_single_pending_write():
    assert server.resolve_voice_confirmation("yes", [w()], []) == \
        {"kind": "write", "token": PW, "approved": True}


def test_bare_no_cancels_the_single_pending_edit():
    v = server.resolve_voice_confirmation("no", [], [e()])
    assert v["kind"] == "edit" and v["approved"] is False and v["phrase"] == ""


def test_bare_words_need_exactly_one_pending_item():
    assert server.resolve_voice_confirmation("yes", [w()], [e()]) is None
    assert server.resolve_voice_confirmation("no", [w()], [e()]) is None
    assert server.resolve_voice_confirmation("yes", [], []) is None


def test_conversation_falls_through_to_the_model():
    # "yes please" is an answer, not a confirmation — nothing pending it can
    # point at, so it must run as a normal turn.
    assert server.resolve_voice_confirmation("yes please", [w()], []) is None
    assert server.resolve_voice_confirmation("how is the weather", [w()], []) is None


def test_unknown_token_shape_gets_a_note_not_a_turn():
    v = server.resolve_voice_confirmation("yes pw_ffeeddccbbaa9988", [w()], [])
    assert v["kind"] == "note" and "not pending" in v["note"]


# ---------------------------------------------------------------------------
# the WebSocket seam
# ---------------------------------------------------------------------------

class RecordingOWW:
    """Scores every frame at 0.0: the wake word never fires in these tests."""

    def __init__(self) -> None:
        self.frames = []

    def predict(self, frame):
        arr = np.asarray(frame)
        # openWakeWord's preprocessor needs int16 amplitudes; a normalized
        # float feed scores 0.0 on real speech, which is exactly the bug
        # that kept the wake word dead while every other stage looked fine.
        assert arr.dtype == np.int16, f"OWW must receive int16, got {arr.dtype}"
        self.frames.append(arr.copy())
        return {"hey jarvis": 0.0}


class StubWhisper:
    """faster-whisper stand-in: transcribe() yields one fixed segment."""

    def __init__(self, text: str = "") -> None:
        self.text = text

    def transcribe(self, audio, **kw):
        seg = type("Seg", (), {"text": self.text})
        return [seg], None


class _StubWatchers:
    def poll_once(self):
        return None


class _StubAlerts:
    def open_alerts(self, n: int):
        return []


class FakeAgentContext:
    """Records every settle call; the real resolver never runs in the test."""

    def __init__(self) -> None:
        self.writes: list[dict] = []
        self.edits: list[dict] = []
        self.write_calls: list[tuple] = []
        self.edit_calls: list[tuple] = []
        self.memory = None
        self.watchers = _StubWatchers()
        self.alerts = _StubAlerts()

    def pending_writes(self):
        return [dict(x) for x in self.writes]

    def pending_edits(self):
        return [dict(x) for x in self.edits]

    def resolve_write(self, token: str, approved: bool):
        self.write_calls.append((token, approved))
        return {"token": token, "approved": approved, "outcome": {"ok": True}}

    def resolve_edit(self, token: str, approved: bool, confirm_phrase: str = ""):
        self.edit_calls.append((token, approved, confirm_phrase))
        return {"token": token, "approved": approved,
                "outcome": {"path": "x.py", "added": 1, "removed": 0}}


@pytest.fixture
def ws_env(monkeypatch):
    """Real app + websocket, with the model seams and agent context stubbed."""
    import silero_vad

    was_ready = server.MODELS.ready
    was_oww = server.MODELS.oww
    was_whisper = server.MODELS.whisper
    was_vad = server.MODELS.vad_model
    was_stamps = silero_vad.get_speech_timestamps

    server.MODELS.ready = True
    oww = RecordingOWW()
    whisper = StubWhisper()
    server.MODELS.oww = oww
    monkeypatch.setattr(server.MODELS, "new_oww", lambda: oww)
    server.MODELS.whisper = whisper
    server.MODELS.vad_model = object()
    silero_vad.get_speech_timestamps = lambda *a, **kw: []

    fake = FakeAgentContext()
    monkeypatch.setattr(server, "agent_context", lambda: fake)
    # Deterministic timing: every mic chunk re-arms the VAD poll, and the
    # window knobs come from the fixture, not a user's settings.json.
    monkeypatch.setattr(server, "VAD_POLL_S", 0.0)
    monkeypatch.setitem(server.SETTINGS, "followup_window_s", 8.0)
    monkeypatch.setitem(server.SETTINGS, "followup_max_streak", 2)
    monkeypatch.setitem(server.SETTINGS, "vad_tail_s", 1.2)
    monkeypatch.setitem(server.SETTINGS, "barge_min_frames", 40)

    env = type("Env", (), {"fake": fake, "whisper": whisper, "oww": oww})()
    app = server.build_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as c:
        yield c, env
    server.MODELS.ready, server.MODELS.oww = was_ready, was_oww
    server.MODELS.whisper, server.MODELS.vad_model = was_whisper, was_vad
    silero_vad.get_speech_timestamps = was_stamps


def silence_chunks(n: int = 4, chunk_samples: int = 320) -> list[str]:
    """n quiet 20 ms mic frames as the browser would send them."""
    out = []
    for _ in range(n):
        pcm = np.zeros(chunk_samples, dtype=np.int16)
        out.append(base64.b64encode(pcm.tobytes()).decode("ascii"))
    return out


def feed_audio(ws, chunks: list[str]) -> None:
    for payload in chunks:
        ws.send_json({"op": "audio", "data": payload})


def wait_for_op(ws, op: str, **want):
    while True:
        m = ws.receive_json()
        if m.get("op") == op and all(m.get(k) == v for k, v in want.items()):
            return m


def test_playback_done_opens_the_followup_window(ws_env):
    c, _env = ws_env
    with c.websocket_connect("/ws") as ws:
        assert ws.receive_json()["op"] == "hello"
        ws.send_json({"op": "playback_done"})
        m = wait_for_op(ws, "state", state="listening")
        assert m["reason"] == "follow-up"


def test_playback_done_clears_barge_state_and_opens_followup(ws_env):
    c, _env = ws_env
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()
        client = next(iter(server.CLIENTS))
        client.playback_active = True
        client.barge_frames = 2
        client.barge_candidate = [np.ones(320, dtype=np.float32)]
        ws.send_json({"op": "playback_done"})
        wait_for_op(ws, "state", state="listening")
        assert client.playback_active is False
        assert client.barge_frames == 0
        assert client.barge_candidate == []


def test_barge_in_requires_sustained_audio_and_reopens_listening(ws_env):
    c, env = ws_env
    env.whisper.text = "hey jarvis"
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()
        client = next(iter(server.CLIENTS))
        client.playback_active = True
        client.emit(client.queue, "state", state="speaking")
        quiet = np.zeros(320, dtype=np.int16)
        loud = np.full(320, 5000, dtype=np.int16)

        ws.send_json({"op": "audio", "data": base64.b64encode(quiet.tobytes()).decode()})
        assert client.playback_active is True
        assert client.barge_frames == 0

        for _ in range(39):
            ws.send_json({"op": "audio", "data": base64.b64encode(loud.tobytes()).decode()})
        assert client.playback_active is True
        assert client.barge_frames == 39

        ws.send_json({"op": "audio", "data": base64.b64encode(loud.tobytes()).decode()})
        # Confirmation is a background transcription, not a synchronous cut:
        # the reply keeps playing until the wake phrase is actually heard.
        m = wait_for_op(ws, "state", state="listening")
        assert m["reason"] == "barge-in"
        assert client.playback_active is False
        assert client.listening is True
        assert client.barge_candidate == []
        # Pre-roll holds 1.5s of audio so the first word of the interruption
        # is not clipped at the threshold.
        assert len(client.buffer) <= 1.5 * 16000 + 320


def test_barge_without_wake_phrase_does_not_cut_off_speech(ws_env):
    c, env = ws_env
    env.whisper.text = "some ordinary words"
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()
        client = next(iter(server.CLIENTS))
        client.playback_active = True
        client.playback_id = "reply"
        client.emit(client.queue, "state", state="speaking")
        loud = np.full(320, 5000, dtype=np.int16)
        for _ in range(40):
            ws.send_json({"op": "audio", "data": base64.b64encode(loud.tobytes()).decode()})
        import time
        deadline = time.monotonic() + 5
        while client.barge_checking and time.monotonic() < deadline:
            time.sleep(0.01)
        ws.send_json({"op": "mute", "on": True})   # flush: proves no barge happened
        wait_for_op(ws, "state", state="muted")
        assert client.playback_active is True
        assert client.playback_id == "reply"
        assert client.listening is False


def test_stale_playback_ack_does_not_end_new_reply(ws_env):
    c, _env = ws_env
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()
        client = next(iter(server.CLIENTS))
        client.playback_active = True
        client.playback_id = "current"
        ws.send_json({"op": "playback_done", "id": "old"})
        ws.send_json({"op": "mute", "on": True})
        wait_for_op(ws, "state", state="muted")
        assert client.playback_active is True
        assert client.playback_id == "current"
        ws.send_json({"op": "playback_done", "id": "current"})
        wait_for_op(ws, "state", state="idle")
        assert client.playback_active is False


def test_barge_in_muted_is_ignored(ws_env):
    c, _env = ws_env
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()
        client = next(iter(server.CLIENTS))
        client.playback_active = True
        client.muted = True
        loud = np.full(320, 5000, dtype=np.int16)
        for _ in range(5):
            ws.send_json({"op": "audio", "data": base64.b64encode(loud.tobytes()).decode()})
        assert client.playback_active is True
        assert client.barge_frames == 0


def test_playback_done_while_muted_goes_idle(ws_env):
    c, _env = ws_env
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"op": "mute", "on": True})
        wait_for_op(ws, "state", state="muted")
        ws.send_json({"op": "playback_done"})
        wait_for_op(ws, "state", state="idle")


def test_silent_followup_windows_rearm_up_to_the_streak_cap(ws_env):
    c, _env = ws_env
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"op": "playback_done"})
        wait_for_op(ws, "state", reason="follow-up")
        client = next(iter(server.CLIENTS))

        # The open window is still under its streak cap: rewind 9 s of
        # silence past the 8 s grace and let the next VAD poll re-arm it.
        client.listen_started -= 9
        client.last_vad = 0.0
        feed_audio(ws, silence_chunks(2))
        wait_for_op(ws, "state", reason="follow-up")
        assert client.followup_streak == 1

        client.listen_started -= 9
        client.last_vad = 0.0
        feed_audio(ws, silence_chunks(2))
        wait_for_op(ws, "state", reason="follow-up")
        assert client.followup_streak == 2

        # Cap reached (followup_max_streak=2): the next silent timeout
        # closes the floor instead of re-arming.
        client.listen_started -= 9
        client.last_vad = 0.0
        feed_audio(ws, silence_chunks(2))
        wait_for_op(ws, "state", state="idle")
        assert client.followup_streak == 0


def test_spoken_yes_settles_a_pending_write_offline(ws_env):
    import silero_vad

    c, env = ws_env
    env.fake.writes = [w()]
    env.whisper.text = f"yes {PW}"
    # A closed utterance needs a speech segment plus the 1.2 s tail: 35,200
    # samples (2.2 s) of quiet with the segment ending at 16,000.
    silero_vad.get_speech_timestamps = lambda *a, **kw: [{"start": 0, "end": 16000}]
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"op": "start"})          # put the client in listening
        feed_audio(ws, silence_chunks(110))
        m = wait_for_op(ws, "write_resolved", token=PW)
        assert m["approved"] is True
        note = wait_for_op(ws, "chat", role="system")
        assert note["text"].startswith("Write saved")
        # The floor re-opens so the user can keep going by voice.
        wait_for_op(ws, "state", reason="follow-up")
    assert env.fake.write_calls == [(PW, True)]
    assert env.fake.edit_calls == []


def test_spoken_no_settles_a_pending_edit_offline(ws_env):
    import silero_vad

    c, env = ws_env
    env.fake.edits = [e()]
    env.whisper.text = f"no {FE}"
    silero_vad.get_speech_timestamps = lambda *a, **kw: [{"start": 0, "end": 16000}]
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"op": "start"})          # put the client in listening
        feed_audio(ws, silence_chunks(110))
        m = wait_for_op(ws, "edit_resolved", token=FE)
        assert m["approved"] is False
    assert env.fake.edit_calls == [(FE, False, "")]
    assert env.fake.write_calls == []


def test_speech_flag_is_reference_counted():
    """Background work stands down while any reply is being spoken; the flag
    must clear again even when two surfaces speak at once."""
    server.SPEECH_ACTIVE.clear()
    server._speech_begin()
    server._speech_begin()
    assert server.SPEECH_ACTIVE.is_set()
    server._speech_end()
    assert server.SPEECH_ACTIVE.is_set()
    server._speech_end()
    assert not server.SPEECH_ACTIVE.is_set()
    server._speech_end()          # unbalanced calls must not go negative
    assert server._speech_refs == 0


def test_reopening_the_window_replays_the_stored_transcript(ws_env, tmp_path, monkeypatch):
    """Closing JARVIS and opening her again used to show an empty panel even
    though SQLite still held every turn - visually indistinguishable from
    amnesia, so the greeting now carries the transcript back."""
    from jarvis.agent.memory import Memory

    c, env = ws_env
    mem = Memory(tmp_path / "memory.db")
    mem.remember_turn("user", "the elevator code is 9 3 1 4, do not lose it",
                      engine="local (Gemini 3.5 Flash)")
    mem.remember_turn("assistant", "Noted. I have pinned it.", engine="local (Gemini 3.5 Flash)")
    mem.remember_turn("system", "internal note that must not be shown")
    mem.remember_turn("user", "x" * 5000)
    monkeypatch.setattr(env.fake, "memory", mem)

    with c.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["op"] == "hello"
        hist = hello["history"]
        assert [t["role"] for t in hist] == ["user", "assistant", "user"]   # system excluded
        assert "9 3 1 4" in hist[0]["text"]
        assert hist[0]["engine"].startswith("local")
        assert hist[0]["ts"] > 0
        assert len(hist[-1]["text"]) <= 2000                                 # clipped for the wire

    # The brain's own context seeds from the same rows, so what is on screen is
    # also what the model can use - and the stored notice reaches neither.
    with c.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["context_turns"] == 3
        me = next(cl for cl in server.CLIENTS if cl.client_id == hello["client_id"])
        assert [t["role"] for t in me.history] == ["user", "assistant", "user"]
