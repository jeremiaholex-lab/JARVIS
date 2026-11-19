from jarvis.agent.turn import run_turn


class FakeTools:
    """One read-tier tool that answers deadlines, mirroring agent.tools."""

    def __init__(self):
        self.calls = []


def _tool_impls(answers):
    class T:
        name = "jarvis_projects_read"

    return [T()], {"jarvis_projects_read": answers}


def make_engine(responses):
    """call_engine(engine, msgs, tools) -> queued response per engine turn."""
    queue = list(responses)
    seen = []

    def call(engine, msgs, tools):
        seen.append((engine, len(msgs), bool(tools)))
        res = queue.pop(0)
        if isinstance(res, Exception):
            raise res
        return res

    call.seen = seen
    return call


def test_tool_call_round_then_final_answer():
    impls = [type("T", (), {"name": "jarvis_projects_read"})()]
    from jarvis.agent import tools as agent_tools

    def fake_execute(tools, name, args):
        fake_execute.calls.append((name, args))
        return '{"due_soon": [{"title": "Report", "due": "Friday"}]}'

    fake_execute.calls = []
    original = agent_tools.execute
    agent_tools.execute = fake_execute
    try:
        engine = make_engine([
            {"content": "", "tool_calls": [
                {"id": "c1", "name": "jarvis_projects_read", "arguments": '{"view":"due_soon"}'}]},
            {"content": "Your report is due Friday.", "usage": {"completion_tokens": 8}},
        ])
        result = run_turn(
            "what is due soon",
            system_prompt="You are JARVIS.",
            memory=[],
            tools=[{"type": "function", "function": {"name": "jarvis_projects_read"}}],
            tool_impls=impls,
            call_engine=engine,
            local_model="qwen3:1.7b",
            cloud_model="agnes-3.0-flash",
        )
    finally:
        agent_tools.execute = original

    assert result.answer == "Your report is due Friday."
    assert result.engine == "local (qwen3:1.7b)"
    assert fake_execute.calls == [("jarvis_projects_read", '{"view":"due_soon"}')]
    # tool result went back as a tool-role message with its call id
    assert engine.seen[1][1] == 4  # system, user, assistant(tool_calls), tool
    names = [s["name"] for s in result.steps]
    assert names == ["tool", "llm"]


def test_weak_local_answer_is_retried_on_cloud_once():
    engine = make_engine([
        {"content": "I don't know."},
        {"content": "Rain forms when water vapour condenses on dust and nuclei."},
    ])
    result = run_turn(
        "how does rain form in the atmosphere exactly",
        system_prompt="You are JARVIS.",
        memory=[{"role": "user", "content": "earlier"}],
        tools=[],
        tool_impls=[],
        call_engine=engine,
        local_model="qwen3:1.7b",
        cloud_model="agnes-3.0-flash",
        cloud_available=True,
    )
    assert result.engine == "cloud (escalated)"
    assert "confidence" in result.route_reason
    assert engine.seen[0][0] == "local"
    assert engine.seen[1][0] == "cloud"


def test_promise_without_action_gets_one_nudge_then_a_real_tool_call():
    """The model said it would look; the nudge makes it actually look."""
    engine = make_engine([
        {"content": "I will look at your screen and tell you what is there."},
        {"content": "", "tool_calls": [
            {"id": "c1", "name": "look_at_screen", "arguments": '{"question":"what error is showing"}'}]},
        {"content": "The screen shows a TypeError on line 12 of server.py."},
    ])
    from jarvis.agent import tools as agent_tools

    original = agent_tools.execute
    agent_tools.execute = lambda tl, name, args: '{"seen": "TypeError on line 12 of server.py"}'
    try:
        result = run_turn(
            "look at my screen and tell me what error is showing",
            system_prompt="You are JARVIS.",
            tools=[{"type": "function", "function": {"name": "look_at_screen"}}],
            tool_impls=[type("T", (), {"name": "look_at_screen"})()],
            call_engine=engine,
            local_model="qwen3:1.7b",
            cloud_model="agnes-3.0-flash",
            cloud_available=False,
        )
    finally:
        agent_tools.execute = original
    assert "TypeError" in result.answer
    assert result.tools_used == ["look_at_screen"]


def test_data_answers_claiming_ignorance_get_one_tool_nudge():
    """The model sometimes says it cannot look without trying — nudge once."""
    engine = make_engine([
        {"content": "I couldn’t find anything about that."},
        {"content": "", "tool_calls": [
            {"id": "c1", "name": "jarvis_projects_read", "arguments": '{"view":"due_soon"}'}]},
        {"content": "Your report is due Friday."},
    ])
    from jarvis.agent import tools as agent_tools

    original = agent_tools.execute
    agent_tools.execute = lambda tl, name, args: '[]'
    try:
        result = run_turn(
            "how many projects do I have and what is next",
            system_prompt="You are JARVIS.",
            tools=[{"type": "function", "function": {"name": "jarvis_projects_read"}}],
            tool_impls=[type("T", (), {"name": "jarvis_projects_read"})()],
            call_engine=engine,
            local_model="qwen3:1.7b",
            cloud_model="agnes-3.0-flash",
            cloud_available=False,
        )
    finally:
        agent_tools.execute = original

    assert result.answer == "Your report is due Friday."
    assert result.tools_used == ["jarvis_projects_read"]


def test_local_engine_failure_falls_back_to_cloud():
    engine = make_engine([
        RuntimeError("Ollama not reachable"),
        {"content": "Cloud answer after local failure."},
    ])
    result = run_turn(
        "hello there friend",
        system_prompt="You are JARVIS.",
        tools=[],
        tool_impls=[],
        call_engine=engine,
        local_model="qwen3:1.7b",
        cloud_model="agnes-3.0-flash",
    )
    assert result.answer == "Cloud answer after local failure."
    assert result.engine == "cloud (fallback)"
    assert "local engine failed" in result.route_reason


def test_no_cloud_key_means_local_failure_is_raised():
    import pytest

    engine = make_engine([RuntimeError("Ollama not reachable")])
    with pytest.raises(RuntimeError):
        run_turn(
            "hello there friend",
            system_prompt="You are JARVIS.",
            tools=[],
            tool_impls=[],
            call_engine=engine,
            local_model="qwen3:1.7b",
            cloud_model="agnes-3.0-flash",
            cloud_available=False,
        )


def test_conversation_memory_is_passed_into_the_prompt():
    engine = make_engine([{"content": "Yes, we talked about that budget."}])
    run_turn(
        "what did I say before",
        system_prompt="You are JARVIS.",
        memory=[{"role": "user", "content": "the budget is 40k"},
                {"role": "assistant", "content": "noted"}],
        tools=[],
        tool_impls=[],
        call_engine=engine,
        local_model="qwen3:1.7b",
        cloud_model="agnes-3.0-flash",
    )
    assert engine.seen[0][1] == 4  # system + 2 memory + current user


def test_past_failures_are_not_replayed_into_the_prompt():
    """Replaying "I couldn't find that" teaches the next turn to say it too."""
    engine = make_engine([{"content": "Your report is due Friday, per your store."}])
    run_turn(
        "how do raindrops form",
        system_prompt="You are JARVIS.",
        memory=[
            {"role": "user", "content": "what is due?"},
            {"role": "assistant", "content": "I couldn't find any information about that."},
            {"role": "user", "content": "and the weather?"},
            {"role": "assistant", "content": "Rain forms from condensation."},
        ],
        tools=[],
        tool_impls=[],
        call_engine=engine,
        local_model="qwen3:1.7b",
        cloud_model="agnes-3.0-flash",
    )
    # system + 3 surviving memory messages (the failure was dropped) + this user turn
    assert engine.seen[0][1] == 5
