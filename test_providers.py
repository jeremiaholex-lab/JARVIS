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
    gemini_body, parse_gemini_response, parse_model_switch,
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


# -------------------------------------------------------------- voice switch
def test_parse_switch_instruction():
    assert parse_model_switch("switch to gemini 3.5") == "gemini35"
    assert parse_model_switch("switch to gemini 3.5 lite") == "gemini25"
    assert parse_model_switch("switch to gemini 3.5 flash lite") == "gemini25"
    assert parse_model_switch("use lite") == "gemini25"
    assert parse_model_switch("go back to lite") == "gemini25"
    assert parse_model_switch("use gemini 2.5 instead") == "gemini25"  # retired alias
    assert parse_model_switch("switch to agnes") == "agnes"
    assert parse_model_switch("go back to the cloud model") == "agnes"
    assert parse_model_switch("switch to automatic") == "auto"
    assert parse_model_switch("go back to auto") == "auto"
    assert parse_model_switch("use the default model") == "auto"
    assert parse_model_switch("jarvis switch to gemini") == "auto"
    assert parse_model_switch("fall back to 2.5") == "gemini25"
    assert parse_model_switch("change your brain to gemini 3.5 flash") == "gemini35"


def test_parse_switch_rejects_negations_and_chatter():
    assert parse_model_switch("don't use gemini 2.5") is None
    assert parse_model_switch("don't switch to gemini 3.5 lite") is None
    assert parse_model_switch("stop using agnes") is None
    assert parse_model_switch("what is gemini 3.5") is None
    assert parse_model_switch("did i tell you to switch to gemini 2.5 yesterday") is None
    assert parse_model_switch("tell me a story about a robot") is None
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
