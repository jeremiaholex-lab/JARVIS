import json

from jarvis.agent import tools as agent_tools
from jarvis.security.tiers import Tier


def test_every_exposed_tool_is_read_or_a_proposer():
    # Stage 2: the only non-READ tool offered is the safe side-effect-free
    # proposer. Anything else in the tool list must still be read-tier.
    from jarvis.agent.tools import _PROPOSE_ONLY_TOOLS
    for tool in agent_tools.build_tools():
        if tool.tier is Tier.READ:
            continue
        assert tool.name in _PROPOSE_ONLY_TOOLS, tool.name
        assert tool.tier is Tier.WRITE, tool.name


def test_schemas_are_openai_function_shaped():
    schemas = agent_tools.tool_schemas(agent_tools.build_tools())
    assert schemas, "orb must have at least one tool"
    for s in schemas:
        assert s["type"] == "function"
        assert {"name", "description", "parameters"} <= set(s["function"])


def test_unknown_tool_is_refused_with_an_explanation():
    out = json.loads(agent_tools.execute(agent_tools.build_tools(), "git_push", "{}"))
    assert "not available" in out["error"]


def test_write_tier_tool_cannot_be_executed_even_if_asked():
    impl = agent_tools.AgentTool(
        name="file_write",
        description="hypothetical write tool",
        parameters={},
        fn=lambda **kw: "should never run",
        tier=Tier.WRITE,
    )
    out = json.loads(agent_tools.execute([impl], "file_write", {"path": "x"}))
    assert "not read-tier" in out["error"]


def test_bad_arguments_and_crashes_become_errors_not_exceptions():
    tl = agent_tools.build_tools()
    clock = next(t for t in tl if t.name == "get_current_time")
    assert "error" in json.loads(agent_tools.execute([clock], "get_current_time", '{"nope":1}'))

    def boom(**kw):
        raise ValueError("kaboom")

    broken = agent_tools.AgentTool("get_current_time", "", {}, boom, Tier.READ)
    out = json.loads(agent_tools.execute([broken], "get_current_time", "{}"))
    assert "kaboom" in out["error"]


def test_google_read_is_exposed_as_read_only_with_scoped_views():
    tl = agent_tools.build_tools()
    tool = next(t for t in tl if t.name == "google_read")
    assert tool.tier is Tier.READ
    assert tool.parameters["properties"]["view"]["enum"] == ["email", "calendar", "tasks"]
    assert "google_read" in {s["function"]["name"] for s in agent_tools.tool_schemas(tl)}


def test_current_time_tool_returns_local_iso_and_weekday():
    tl = agent_tools.build_tools()
    clock = next(t for t in tl if t.name == "get_current_time")
    payload = json.loads(agent_tools.execute(tl, "get_current_time", "{}"))
    assert "T" in payload["iso"] and payload["weekday"]
