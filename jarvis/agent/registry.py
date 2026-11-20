"""Validated registry for agent tools.

Tool implementations are trusted code, but registration is not a permission
bypass: every tool must agree with the deterministic security classifier.
Optional extensions are READ-only and must be explicitly listed in
``jarvis.security.tiers`` before they can be registered or executed.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from jarvis.agent.tools import AgentTool
from jarvis.security.tiers import PlannedAction, Tier, classify

# Non-read capabilities are deliberately not extensible through the plugin
# interface. Changes to these require editing the confirmation/execution path.
_BUILTIN_NON_READ = {
    "jarvis_projects_write": Tier.WRITE,
    "jarvis_file_edit": Tier.DESTRUCTIVE,
    "jarvis_remember": Tier.WRITE,
}
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ToolRegistry:
    """Unique tool catalog that validates schemas and permission tiers."""

    def __init__(self) -> None:
        self._tools: dict[str, AgentTool] = {}

    def register(self, tool: AgentTool, *, builtin: bool = False) -> None:
        if not isinstance(tool.name, str) or not _NAME.fullmatch(tool.name):
            raise ValueError("tool name must be a lowercase identifier (max 64 characters)")
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        if not callable(tool.fn):
            raise ValueError(f"tool {tool.name!r} has no callable implementation")
        if not isinstance(tool.description, str) or not tool.description.strip():
            raise ValueError(f"tool {tool.name!r} needs a description")
        if not isinstance(tool.parameters, dict) or tool.parameters.get("type") != "object":
            raise ValueError(f"tool {tool.name!r} parameters must be a JSON-schema object")
        properties = tool.parameters.get("properties", {})
        required = tool.parameters.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise ValueError(f"tool {tool.name!r} has invalid parameter schema")
        if any(key not in properties for key in required):
            raise ValueError(f"tool {tool.name!r} requires an undeclared parameter")

        actual = classify(PlannedAction(tool=tool.name, params={}))
        if actual is not tool.tier:
            raise ValueError(
                f"tool {tool.name!r} declares {tool.tier.value}, "
                f"but security policy classifies it as {actual.value}"
            )
        if actual is not Tier.READ:
            if not builtin or _BUILTIN_NON_READ.get(tool.name) is not actual:
                raise ValueError("extensions may register only security-classified READ tools")
        self._tools[tool.name] = tool

    def register_many(self, tools: Iterable[AgentTool], *, builtin: bool = False) -> None:
        # Validate the complete batch before mutating the registry.
        batch = list(tools)
        names = [tool.name for tool in batch]
        if len(names) != len(set(names)):
            raise ValueError("duplicate tool name in registration batch")
        overlap = set(names) & self._tools.keys()
        if overlap:
            raise ValueError(f"duplicate tool name: {sorted(overlap)[0]}")
        candidate = ToolRegistry()
        candidate._tools = self._tools.copy()
        for tool in batch:
            candidate.register(tool, builtin=builtin)
        self._tools = candidate._tools

    def get(self, name: str) -> AgentTool | None:
        return self._tools.get(name)

    def list(self) -> list[AgentTool]:
        return list(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)
