import json

from jarvis.agent import tools as agent_tools
from jarvis.security.tiers import Tier


def test_microsoft_tool_is_read_only_and_scoped():
    available = agent_tools.build_tools()
    tool = next(t for t in available if t.name == "microsoft365_read")
    assert tool.tier is Tier.READ
    assert tool.parameters["properties"]["view"]["enum"] == ["email", "calendar", "tasks"]
    assert "microsoft365_read" in {s["function"]["name"] for s in agent_tools.tool_schemas(available)}


def test_microsoft_read_returns_configuration_guidance_without_network(monkeypatch):
    from jarvis.skills.microsoft_365 import Microsoft365Connector

    monkeypatch.delenv("JARVIS_MICROSOFT_CLIENT_ID", raising=False)
    monkeypatch.setattr(Microsoft365Connector, "_access_token", lambda self: (_ for _ in ()).throw(
        RuntimeError("Microsoft 365 is not configured. Set JARVIS_MICROSOFT_CLIENT_ID.")))
    result = json.loads(agent_tools.execute(agent_tools.build_tools(), "microsoft365_read", {"view": "email"}))
    assert "not configured" in result["error"]
