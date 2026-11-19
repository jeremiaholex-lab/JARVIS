"""Microsoft 365 is deliberately NOT offered to the conversation.

The connector (jarvis.skills.microsoft_365) and its device-code setup script
remain in the tree so the option can be re-enabled by re-adding the tool to
build_tools(); what this test pins is that it is absent from the catalog.
"""
from jarvis.agent import tools as agent_tools


def test_microsoft_tool_is_not_offered():
    names = {t.name for t in agent_tools.build_tools()}
    assert "microsoft365_read" not in names


def test_microsoft_schema_is_not_offered():
    names = {s["function"]["name"] for s in agent_tools.tool_schemas(agent_tools.build_tools())}
    assert "microsoft365_read" not in names
