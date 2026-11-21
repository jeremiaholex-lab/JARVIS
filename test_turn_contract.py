from __future__ import annotations

from jarvis.agent.turn import run_turn


def test_run_turn_passes_the_configured_token_limit_to_the_engine():
    seen = {}

    def call_engine(engine, msgs, tools, max_tokens=None):
        seen["max_tokens"] = max_tokens
        return {"content": "done"}

    run_turn(
        "hello there",
        system_prompt="You are JARVIS.",
        tools=[],
        tool_impls=[],
        call_engine=call_engine,
        local_model="qwen3:1.7b",
        cloud_model="agnes",
    )

    assert seen["max_tokens"] == 400


def test_tool_guidance_does_not_repeat_the_time_instruction():
    from jarvis.agent.turn import TOOL_GUIDANCE

    assert TOOL_GUIDANCE.count("Use a tool") == 1
