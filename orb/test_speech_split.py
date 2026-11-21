from __future__ import annotations

import pytest
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server


def test_short_answers_are_one_sentence():
    assert server.split_sentences("The capital is Paris.") == ["The capital is Paris."]


def test_all_sentences_are_preserved_for_speech():
    long_answer = " ".join(f"Sentence number {i} explains something useful." for i in range(12))
    spoken = server.split_sentences(long_answer)
    assert len(spoken) == 12
    assert " ".join(spoken) == long_answer


def test_punctuation_keeps_following_words_and_sentences():
    text = "Try this: first, second; third. Then another! Finally? Done."
    spoken = server.split_sentences(text)
    assert " ".join(spoken) == text
    assert spoken == ["Try this: first, second; third.", "Then another!", "Finally?", "Done."]


def test_long_sentence_chunks_preserve_punctuation_and_words():
    text = " ".join([*("word," for _ in range(40)), "finish.", "Afterwards, keep going!"])
    spoken = server.split_sentences(text)
    assert " ".join(spoken) == text
    assert all(len(piece) <= 150 for piece in spoken)


def test_empty_answer_speaks_nothing():
    assert server.split_sentences("") == []
    assert server.split_sentences("   ") == []


def test_long_unpunctuated_text_is_split_without_dropping_words():
    text = " ".join(f"word{i}" for i in range(100))
    spoken = server.split_sentences(text)
    assert len(spoken) > 1
    assert all(len(piece) <= 150 for piece in spoken)
    assert " ".join(spoken) == text


def test_explicit_speech_summary_still_trims_at_sentence_boundary():
    text = "Alpha beta gamma. " * 30
    out = server.speech_text(text)
    assert len(out) <= 260
    assert out.endswith(".")


def test_opening_piece_is_short_enough_to_start_speaking_early():
    """Synthesis is the slow stage, so the first sentence is deliberately cut
    shorter than the rest: it is what stands between the answer and the first
    sound."""
    long_first = ("The interface uses a four-column grid with a sidebar, a header, "
                  "a main panel and a footer for status. Then a second sentence.")
    chunks = server.speech_chunks(long_first)
    assert len(chunks[0]) <= 80
    assert " ".join(chunks) == " ".join(server.split_sentences(long_first))


def test_short_replies_are_not_reshaped():
    assert server.speech_chunks("Yes. Certainly.") == ["Yes.", "Certainly."]


def test_wav_seconds_measures_real_audio():
    import io, wave, numpy as np
    buf = io.BytesIO()
    with wave.open(buf, "wb") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(24000)
        f.writeframes(np.zeros(24000, dtype=np.int16).tobytes())
    assert round(server._wav_seconds(buf.getvalue()), 3) == 1.0
    assert server._wav_seconds(b"not a wav") == 0.0


# ------------------------------------------------------------- voice backends
class LocalTTS:
    def __init__(self):
        self.calls = 0

    def synthesize(self, text, **kw):
        self.calls += 1
        return type("R", (), {"audio": b"LOCAL-WAV"})()


@pytest.fixture
def voice_env(monkeypatch):
    local = LocalTTS()
    monkeypatch.setattr(server.MODELS, "tts", local, raising=False)
    monkeypatch.setitem(server.SETTINGS, "voice", "af_heart")
    monkeypatch.setitem(server.SETTINGS, "tts_speed", 1.0)
    monkeypatch.setitem(server.SETTINGS, "voice_backend", "auto")
    monkeypatch.setattr(server, "_tts_cooldown_until", 0.0)
    monkeypatch.delitem(server.SETTINGS, "crowllm_tts", raising=False)
    monkeypatch.delitem(server.ENV, "CROWLLM_API_KEY", raising=False)
    calls = []

    def fake_hosted(text, *, key, voice_id="af_heart", **kwargs):
        calls.append((text, key, voice_id))
        if "boom" in text:
            raise RuntimeError("hosted voice down")
        if "quota" in text:
            from jarvis.agent.providers import RateLimited
            raise RateLimited("quota")
        return b"HOSTED-WAV"

    class FakeProviders:
        RateLimited = __import__(
            "jarvis.agent.providers", fromlist=["RateLimited"]).RateLimited

        @staticmethod
        def gemini_tts(text, *, key, voice_id="af_heart", **kwargs):
            return fake_hosted(text, key=key, voice_id=voice_id)

    monkeypatch.setattr(server, "_provider_module", lambda: FakeProviders)
    monkeypatch.setitem(server.ENV, "GEMINI_API_KEY", "gk")
    return local, calls


def test_auto_voice_prefers_the_hosted_backend(voice_env):
    local, calls = voice_env
    server.SETTINGS["voice_backend"] = "auto"
    try:
        wav, _ms = server.synthesize("hello")
        assert wav == b"HOSTED-WAV" and server._last_voice_backend == "gemini"
    finally:
        server.SETTINGS["voice_backend"] = "auto"
    assert local.calls == 0


def test_local_only_never_calls_the_hosted_backend(voice_env):
    local, calls = voice_env
    server.SETTINGS["voice_backend"] = "local"
    try:
        wav, _ms = server.synthesize("hello")
    finally:
        server.SETTINGS["voice_backend"] = "auto"
    assert wav == b"LOCAL-WAV" and calls == []


def test_hosted_failure_falls_back_to_the_local_voice(voice_env):
    local, calls = voice_env
    wav, _ms = server.synthesize("boom goes the voice")
    assert wav == b"LOCAL-WAV"
    assert server._last_voice_backend == "local" and local.calls == 1


def test_auto_voice_uses_local_when_no_key_exists(voice_env, monkeypatch):
    local, calls = voice_env
    server.SETTINGS["voice_backend"] = "auto"
    monkeypatch.setitem(server.ENV, "GEMINI_API_KEY", "")
    monkeypatch.delitem(server.ENV, "GEMINI_API_KEY_2", raising=False)
    try:
        assert server.synthesize("hello")[0] == b"LOCAL-WAV"
    finally:
        server.SETTINGS["voice_backend"] = "auto"
    assert calls == []


def test_voice_hosted_decision_follows_settings_and_keys(voice_env, monkeypatch):
    local, calls = voice_env
    for backend, want in (("gemini", True), ("local", False), ("", False)):
        server.SETTINGS["voice_backend"] = backend
        assert server._voice_is_hosted() is want, backend
    server.SETTINGS["voice_backend"] = "auto"


def test_hosted_quota_exhaustion_rests_instead_of_retrying_every_piece(voice_env):
    """A free TTS key 429s after a few requests. Falling back is right; falling
    back after a failed round trip for EVERY sentence is slower than the local
    voice alone, so the rejection parks the hosted path for a while."""
    local, calls = voice_env
    wav, _ms = server.synthesize("quota exceeded please wait")
    assert wav == b"LOCAL-WAV" and server._last_voice_backend == "local"
    assert server._tts_cooldown_until > time.time()
    assert server.voice_status()["hosted_available"] is False
    calls.clear()
    server.synthesize("next sentence")
    assert calls == []                      # no further hosted attempts
    assert local.calls == 2
    # an explicit instruction overrides the rest
    server.SETTINGS["voice_backend"] = "gemini"
    try:
        assert server._voice_is_hosted() is True
    finally:
        server.SETTINGS["voice_backend"] = "auto"


def test_smooth_pacing_is_one_gapless_piece():
    long_answer = " ".join(f"Sentence number {i} explains something useful." for i in range(8))
    smooth = server.speech_chunks(long_answer, pacing="smooth")
    quick = server.speech_chunks(long_answer, pacing="quick")
    assert len(smooth) == 1
    assert len(quick) > 3
    assert smooth[0] == " ".join(quick)          # same words, different pacing
    assert len(quick[0]) <= 80


def test_spoken_length_caps_only_the_voice_not_the_answer():
    long_answer = " ".join(f"Sentence number {i} explains something useful." for i in range(12))
    capped = server.speech_chunks(long_answer, pacing="smooth", max_chars=200)
    assert len(capped[0]) <= 200 + len(" The rest is on screen.")
    assert capped[0].endswith("The rest is on screen.")
    assert server.speech_chunks("Short answer.", pacing="smooth", max_chars=200) == ["Short answer."]
