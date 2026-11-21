from __future__ import annotations

import pytest

from jarvis.agent.registry import ToolRegistry
from jarvis.agent.tools import AgentTool, build_tools
from jarvis.security.tiers import Tier


def tool(name="get_current_time", *, tier=Tier.READ, fn=None, parameters=None):
    return AgentTool(
        name=name,
        description="Test tool.",
        parameters=parameters or {"type": "object", "properties": {}},
        fn=fn or (lambda: "ok"),
        tier=tier,
    )


def test_standard_catalog_registers_and_keeps_names_unique():
    tools = build_tools()
    registry = ToolRegistry()
    registry.register_many(tools, builtin=True)
    assert len(registry) == len(tools)
    assert len({item.name for item in registry.list()}) == len(tools)
    assert registry.get("jarvis_task_schedule") is not None


def test_extension_must_be_known_read_tier():
    registry = ToolRegistry()
    registry.register(tool())
    assert registry.get("get_current_time").fn() == "ok"
    with pytest.raises(ValueError, match="security policy classifies"):
        registry.register(tool("arbitrary_extension"))


def test_extensions_cannot_add_write_capabilities():
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="READ tools"):
        registry.register(tool("jarvis_projects_write", tier=Tier.WRITE))


def test_duplicate_registration_is_rejected_without_replacing_tool():
    registry = ToolRegistry()
    original = tool()
    registry.register(original)
    with pytest.raises(ValueError, match="duplicate tool name"):
        registry.register(tool(fn=lambda: "replaced"))
    assert registry.get(original.name) is original


def test_batch_registration_is_atomic_on_invalid_entry():
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="security policy classifies"):
        registry.register_many([tool(), tool("arbitrary_extension")])
    assert len(registry) == 0


def test_required_schema_properties_must_be_declared():
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="undeclared parameter"):
        registry.register(tool(parameters={
            "type": "object", "properties": {}, "required": ["missing"]
        }))
