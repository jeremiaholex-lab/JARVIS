"""Regression tests for CrowLLM speech integration."""
from __future__ import annotations

import base64
import io
import wave

from jarvis.agent import providers


def _wav() -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(24000)
        f.writeframes(b"\x00\x00" * 100)
    return out.getvalue()


def test_crowllm_speech_posts_openai_audio_body_and_returns_wav():
    """CrowLLM speech must use the configured voice and return playable WAV."""
    calls = []
    expected = _wav()

    def fake_post(url, headers, body, timeout_s):
        calls.append((url, headers, body, timeout_s))
        return 200, expected

    result = providers.crowllm_tts(
        "Hello there.",
        key="test-key",
        base_url="https://example.test/v1",
        model="tts-bruce-en",
        post=fake_post,
    )

    assert result == expected
    assert len(calls) == 1
    url, headers, body, timeout_s = calls[0]
    assert url == "https://example.test/v1/audio/speech"
    assert headers["Authorization"] == "Bearer test-key"
    assert headers["Content-Type"] == "application/json"
    assert body["model"] == "tts-bruce-en"
    assert body["input"] == "Hello there."
    assert body["response_format"] == "wav"
    assert timeout_s > 0


def test_crowllm_speech_accepts_base64_json_response():
    """Some deployments wrap WAV bytes in a JSON audio field."""
    encoded = base64.b64encode(_wav()).decode("ascii")

    def fake_post(url, headers, body, timeout_s):
        return 200, {"audio": encoded}

    result = providers.crowllm_tts("Hello.", key="test-key", post=fake_post)
    assert result.startswith(b"RIFF")


def test_crowllm_speech_raises_rate_limited():
    """429 must trigger the existing hosted-voice cooldown path."""

    def fake_post(url, headers, body, timeout_s):
        return 429, {"error": {"message": "quota"}}

    try:
        providers.crowllm_tts("Hello.", key="test-key", post=fake_post)
    except providers.RateLimited:
        pass
    else:
        raise AssertionError("RateLimited was not raised")


def test_crowllm_tts_helper_preserves_raw_audio_bytes():
    """The shared HTTP helper must not decode WAV bytes as UTF-8 text."""
    from jarvis.agent import providers

    class Response:
        status_code = 200
        content = _wav()

        def json(self):
            raise ValueError("not json")

        @property
        def text(self):
            return self.content.decode("utf-8", "replace")

    class ApiError(Exception):
        pass

    class TimeoutException(ApiError):
        pass

    class HTTPError(ApiError):
        pass

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return Response()

    import sys
    fake_httpx = type("H", (), {
        "Client": Client,
        "TimeoutException": TimeoutException,
        "HTTPError": HTTPError,
    })()
    original = sys.modules.get("httpx")
    sys.modules["httpx"] = fake_httpx
    try:
        status, data = providers._httpx_post(
            "https://example.test/audio", {}, {}, 5.0, raw=True)
    finally:
        if original is None:
            sys.modules.pop("httpx", None)
        else:
            sys.modules["httpx"] = original

    assert status == 200
    assert data == Response.content
