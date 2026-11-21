"""The hosted model ladder: conversion, fallback, cooldowns, voice switching."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis.agent.providers import (  # noqa: E402
    ModelLadder, ProviderError, RateLimited, Tier,
    gemini_body, gemini_tts, parse_gemini_response, parse_model_switch,
)


def tier(tid, kind="gemini", key="k", **kw):
    return Tier(id=tid, label=tid, kind=kind, model=kw.pop("model", tid + "-model"),
                key=key, base_url=kw.pop("base_url", "https://x/v1"), **kw)


def ladder_with(*tiers, post=None, pref_path=None, cooldown_s=120.0):
    return ModelLadder(list(tiers), pref_path=pref_path, cooldown_s=cooldown_s,
                       post=post or (lambda *a, **k: (_ for _ in ()).throw(AssertionError("no post"))))


# ------------------------------------------------------------- gemini adapter
def test_gemini_body_conversion():
    msgs = [
        {"role": "system", "content": "You are JARVIS."},
        {"role": "system", "content": "Extra block."},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "get_current_time", "name": "get_current_time", "arguments": "{}"}]},
        {"role": "tool", "tool_call_id": "get_current_time", "content": '{"time": "3pm"}'},
        {"role": "assistant", "content": "It is 3pm."},
    ]
    tools = [{"type": "function", "function": {
        "name": "get_current_time", "description": "now",
        "parameters": {"type": "object", "properties": {
            "zone": {"type": "string", "enum": ["utc", "local"]},
            "deep": {"type": "object", "properties": {"n": {"type": "integer"}}}},
            "required": ["zone"]}}}]
    body = gemini_body(msgs, tools, max_tokens=200)
    assert body["systemInstruction"]["parts"][0]["text"] == "You are JARVIS.\n\nExtra block."
    roles = [c["role"] for c in body["contents"]]
    assert roles == ["user", "model", "user", "model"]
    fcall = body["contents"][1]["parts"][0]["functionCall"]
    assert fcall == {"name": "get_current_time", "args": {}}
    fresp = body["contents"][2]["parts"][0]["functionResponse"]
    assert fresp["name"] == "get_current_time"
    assert fresp["response"] == {"time": "3pm"}
    decl = body["tools"][0]["functionDeclarations"][0]
    assert decl["parameters"]["type"] == "OBJECT"
    assert decl["parameters"]["properties"]["zone"]["enum"] == ["utc", "local"]
    assert "type" not in decl["parameters"]["properties"]["zone"]
    assert decl["parameters"]["required"] == ["zone"]
    assert body["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 0


def test_gemini_response_parses_text_and_calls():
    data = {"candidates": [{"content": {"parts": [
        {"text": "Sure. "},
        {"functionCall": {"name": "read_file", "args": {"path": "a.txt"}}},
    ]}}], "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 7}}
    out = parse_gemini_response(data)
    assert out["content"] == "Sure."
    assert out["tool_calls"][0]["name"] == "read_file"
    assert json.loads(out["tool_calls"][0]["arguments"]) == {"path": "a.txt"}
    assert out["usage"]["prompt_tokens"] == 5


def test_gemini_blocked_prompt_raises():
    with pytest.raises(ProviderError):
        parse_gemini_response({"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}})


# ---------------------------------------------------------------- ladder order
def test_order_skips_keyless_tiers():
    lad = ladder_with(tier("gemini35"), tier("gemini25", key=None), tier("agnes", kind="openai"))
    assert [t.id for t in lad.order()] == ["gemini35", "agnes"]


def test_deep_purpose_puts_agnes_first():
    lad = ladder_with(tier("gemini35"), tier("agnes", kind="openai"))
    assert [t.id for t in lad.order("deep")] == ["agnes", "gemini35"]


def test_preference_pins_lead_but_ladder_stays_behind(tmp_path):
    lad = ladder_with(tier("gemini35"), tier("gemini25"), tier("agnes", kind="openai"),
                      pref_path=tmp_path / "pref.json")
    lad.set_preference("gemini25")
    assert [t.id for t in lad.order()] == ["gemini25", "gemini35", "agnes"]
    # preference survives a restart of the ladder object
    again = ModelLadder(lad.tiers, pref_path=tmp_path / "pref.json")
    assert again.preference == "gemini25"


def test_unknown_preference_is_discarded(tmp_path):
    (tmp_path / "pref.json").write_text('{"pref": "gpt-99"}')
    lad = ModelLadder([tier("gemini35")], pref_path=tmp_path / "pref.json")
    assert lad.preference == "auto"


# ------------------------------------------------------------------- fallbacks
def test_rate_limit_parks_tier_and_falls_to_next():
    calls = []

    def post(url, headers, body, timeout_s):
        calls.append(url)
        if "gemini35" in url:
            return 429, {"error": {"message": "Resource has been exhausted (quota)."}}
        return 200, {"candidates": [{"content": {"parts": [{"text": "from 2.5"}]}}]}

    lad = ladder_with(tier("gemini35"), tier("gemini25"), post=post, cooldown_s=60)
    t, resp = lad.call([{"role": "user", "content": "hi"}], None)
    assert t.id == "gemini25" and resp["content"] == "from 2.5"
    assert lad.tier("gemini35").cooling
    # next turn skips the parked tier without even trying it
    calls.clear()
    lad.call([{"role": "user", "content": "hi"}], None)
    assert all("gemini35" not in u for u in calls)


def test_cooldown_expires():
    def post(url, headers, body, timeout_s):
        if "gemini35" in url:
            return 429, {"error": {"message": "quota"}}
        return 200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}

    lad = ladder_with(tier("gemini35"), tier("gemini25"), post=post, cooldown_s=0.05)
    lad.call([{"role": "user", "content": "hi"}], None)
    urls = []

    def post2(url, headers, body, timeout_s):
        urls.append(url)
        return 200, {"candidates": [{"content": {"parts": [{"text": "back"}]}}]}

    lad._post = post2
    time.sleep(0.06)
    t, _ = lad.call([{"role": "user", "content": "hi"}], None)
    assert t.id == "gemini35"


def test_all_cooling_still_probes_last_resort():
    def post(url, headers, body, timeout_s):
        return 429, {"error": {"message": "quota"}}

    lad = ladder_with(tier("gemini35"), tier("agnes", kind="openai"), post=post)
    with pytest.raises(RateLimited):
        lad.call([{"role": "user", "content": "hi"}], None)
    # both parked; the ladder must still try the LAST tier (someone has to answer)
    tried = []

    def post2(url, headers, body, timeout_s):
        tried.append(url)
        return 200, {"choices": [{"message": {"content": "agnes saves the turn"}}]}

    lad._post = post2
    t, resp = lad.call([{"role": "user", "content": "hi"}], None)
    assert t.id == "agnes" and tried and tried[0].endswith("/chat/completions")


def test_agnes_openai_parse_roundtrip():
    def post(url, headers, body, timeout_s):
        assert body["tools"][0]["function"]["name"] == "web_search"
        return 200, {"choices": [{"message": {
            "content": None,
            "tool_calls": [{"id": "call_9", "function": {"name": "web_search",
                                                         "arguments": "{\"q\":\"x\"}"}}]}}]}

    lad = ladder_with(tier("agnes", kind="openai"), post=post)
    t, resp = lad.call([{"role": "user", "content": "hi"}],
                       [{"type": "function", "function": {"name": "web_search"}}])
    assert resp["tool_calls"][0]["name"] == "web_search"
    assert resp["tool_calls"][0]["id"] == "web_search"  # id==name: tool_call_id maps back


def test_no_tiers_configured_explains_the_fix():
    lad = ladder_with(tier("gemini35", key=None))
    with pytest.raises(ProviderError) as e:
        lad.call([{"role": "user", "content": "hi"}], None)
    assert "GEMINI_API_KEY" in str(e.value)


def test_thinking_budget_rejected_retries_without_it():
    seen = []

    def post(url, headers, body, timeout_s):
        seen.append(dict(body["generationConfig"]))
        if len(seen) == 1:
            return 400, {"error": {"message": "Unsupported setting: thinkingConfig"}}
        return 200, {"candidates": [{"content": {"parts": [{"text": "no-thinking works"}]}}]}

    lad = ladder_with(tier("gemini35"), post=post)
    t, resp = lad.call([{"role": "user", "content": "hi"}], None)
    assert resp["content"] == "no-thinking works"
    assert "thinkingConfig" not in seen[1]


def test_thinking_rejection_is_remembered_per_tier():
    """Flash-Lite 400s on thinkingConfig; paying that wasted round trip on
    every call is exactly the "it takes ages to answer" the owner hears."""
    seen = []

    def post(url, headers, body, timeout_s):
        seen.append(dict(body["generationConfig"]))
        if len(seen) == 1:
            return 400, {"error": {"message": "Unsupported setting: thinkingConfig"}}
        return 200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}

    lad = ladder_with(tier("gemini35"), post=post)
    lad.call([{"role": "user", "content": "hi"}], None)
    _t, resp = lad.call([{"role": "user", "content": "again"}], None)
    assert resp["content"] == "ok"
    assert "thinkingConfig" not in seen[2]      # second call never sends it


def test_thought_signature_round_trips_through_a_tool_call():
    """Gemini 3.x issues a signature with each functionCall and 400s the
    tool-result turn that fails to echo it back."""
    parsed = parse_gemini_response({"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "jarvis_projects_read", "args": {}},
         "thoughtSignature": "sig_abc"}]}}]})
    call = parsed["tool_calls"][0]
    assert call["thought_signature"] == "sig_abc"

    body = gemini_body([
        {"role": "user", "content": "list projects"},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "jarvis_projects_read",
         "content": json.dumps({"projects": []})},
    ], [{"function": {"name": "jarvis_projects_read", "description": "",
                      "parameters": {"type": "object", "properties": {}}}}])
    echoed = body["contents"][1]["parts"][0]["functionCall"]
    assert echoed["thoughtSignature"] == "sig_abc"


# -------------------------------------------------------------- slash switch
def test_slash_commands_switch():
    assert parse_model_switch("/3.5") == "gemini35"
    assert parse_model_switch("/3.5 lite") == "gemini25"
    assert parse_model_switch("/lite") == "gemini25"
    assert parse_model_switch("/flash lite") == "gemini25"
    assert parse_model_switch("/2.5") == "gemini25"       # retired-name alias
    assert parse_model_switch("/agnes") == "agnes"
    assert parse_model_switch("/cloud") == "agnes"
    assert parse_model_switch("/auto") == "auto"
    assert parse_model_switch("/default") == "auto"
    assert parse_model_switch("/gemini") == "auto"
    assert parse_model_switch("/help") == "help"


def test_slash_spoken_and_wake_prefixed():
    assert parse_model_switch("slash 3.5 lite") == "gemini25"
    assert parse_model_switch("hey jarvis /3.5") == "gemini35"
    assert parse_model_switch("jarvis /lite") == "gemini25"
    assert parse_model_switch("/3.5!") == "gemini35"
    assert parse_model_switch("/LITE") == "gemini25"


def test_nothing_without_the_slash_switches_anymore():
    """The owner retired the natural-language path on purpose: verbs, bare
    aliases and questions are all ordinary conversation now."""
    assert parse_model_switch("switch to gemini 3.5 lite") is None
    assert parse_model_switch("use agnes") is None
    assert parse_model_switch("3.5 lite") is None
    assert parse_model_switch("auto") is None
    assert parse_model_switch("don't use /3.5") is None  # slash must be the prefix
    assert parse_model_switch("what is /lite") is None
    assert parse_model_switch("tell me a joke about a robot") is None
    assert parse_model_switch("") is None


def test_status_shape():
    lad = ladder_with(tier("gemini35"), tier("gemini25", key=None))
    st = lad.status()
    assert st["preference"] == "auto"
    assert st["tiers"][0]["available"] is True
    assert st["tiers"][1]["available"] is False


def test_openai_body_normalizes_compact_tool_calls():
    """The live finding: turn.py echoes compact {id,name,arguments} calls and
    history rows carry an engine key; strict endpoints reject both."""
    from jarvis.agent.providers import _openai_clean_msgs
    seen = {}

    def post(url, headers, body, timeout_s):
        seen.update(body)
        return 200, {"choices": [{"message": {"content": "8am"}}]}

    lad = ladder_with(tier("agnes", kind="openai"), post=post)
    msgs = [
        {"role": "user", "content": "what is my alarm", "engine": "whisper"},
        {"role": "assistant", "content": "", "engine": "local",
         "tool_calls": [{"id": "get_alarm", "name": "get_alarm", "arguments": "{}"}]},
        {"role": "tool", "tool_call_id": "get_alarm", "content": '{"at": "8am"}'},
    ]
    t, resp = lad.call(msgs, None)
    assert resp["content"] == "8am"
    m1 = seen["messages"][1]
    assert "engine" not in m1
    tc = m1["tool_calls"][0]
    assert tc["type"] == "function" and tc["function"]["name"] == "get_alarm"
    assert seen["messages"][2]["tool_call_id"] == "get_alarm"


def test_bare_alias_words_switch_without_a_verb():
    """Superseded by the slash ruling - kept as the negative pin."""
    assert parse_model_switch("3.5") is None
    assert parse_model_switch("/3.5") == "gemini35"


def test_bare_aliases_require_the_whole_message():
    assert parse_model_switch("what is 3.5 lite") is None
    assert parse_model_switch("agnes is nice") is None
    assert parse_model_switch("/agnes is nice") is None


# ---------------------------------------------------------------- hosted voice
def test_gemini_tts_wraps_pcm_in_a_wav():
    import base64
    pcm = (np.zeros(24000, dtype=np.int16)).tobytes() if "np" in dir() else b"\x00\x00" * 24000
    seen = {}

    def post(url, headers, body, timeout_s):
        seen.update({"url": url, "body": body})
        return 200, {"candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "audio/L16;codec=pcm;rate=24000",
                            "data": base64.b64encode(pcm).decode()}}]}}]}

    wav = gemini_tts("Hello there.", key="k", voice_id="af_heart", post=post)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert seen["url"].endswith("gemini-2.5-flash-preview-tts:generateContent")
    assert seen["body"]["generationConfig"]["responseModalities"] == ["AUDIO"]
    # the Kokoro voice id maps to a Gemini preset instead of being dropped
    assert seen["body"]["generationConfig"]["speechConfig"]["voiceConfig"] == {
        "prebuiltVoiceConfig": {"voiceName": "Kore"}}


def test_gemini_tts_errors_are_ladder_shaped():
    import base64
    def post(url, headers, body, timeout_s):
        if body["contents"][0]["parts"][0]["text"].startswith("rate"):
            return 429, {"error": {"message": "quota"}}
        if body["contents"][0]["parts"][0]["text"].startswith("bad"):
            return 400, {"error": {"message": "voice not found"}}
        return 200, {"candidates": [{"content": {"parts": []}}]}

    with pytest.raises(RateLimited):
        gemini_tts("rate me", key="k", post=post)
    with pytest.raises(ProviderError):
        gemini_tts("bad voice", key="k", post=post)
    with pytest.raises(ProviderError):
        gemini_tts("silent", key="k", post=post)
    assert gemini_tts("   ", key="k", post=post) == b""
