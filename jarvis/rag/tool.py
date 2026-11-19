"""Agent-facing RAG search tool (JARVIS-side registration).

Registers ``jarvis_rag_search`` with the framework's ToolRegistry so any
agent turn can search the user's indexed documents and receive ranked,
cited chunks. JARVIS-side glue: import this module before the ask flow
builds its tool list (see jarvis/rag/runner.py) — no framework edits.
"""
from __future__ import annotations

import json
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec


def _store():
    from openjarvis.core.paths import get_config_dir

    from jarvis.rag.setup import default_folders

    from .store import RagStore

    return RagStore(get_config_dir() / "rag.db", folders=default_folders())


@ToolRegistry.register("jarvis_rag_search")
class JarvisRagSearchTool(BaseTool):
    tool_id = "jarvis_rag_search"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.tool_id,
            description=(
                "Search the JARVIS document index (the user's own folders, "
                "embedded locally) and return ranked snippets with source "
                "citations. Read-only. Use when the answer may live in a "
                "document the user has indexed."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural-language question or search terms."},
                    "top_k": {"type": "integer", "description": "How many hits to return (max 10). Default 5."},
                },
                "required": ["query"],
            },
            category="memory",
            required_capabilities=["memory:read"],
            requires_confirmation=False,  # read-only: free tier
        )

    def execute(self, **params: Any) -> ToolResult:
        query = str(params.get("query", "")).strip()
        if not query:
            return ToolResult(tool_name=self.tool_id, content="No query provided.", success=False)
        top_k = min(int(params.get("top_k") or 5), 10)
        try:
            hits = _store().search(query, top_k=top_k)
        except FileNotFoundError:
            return ToolResult(
                tool_name=self.tool_id,
                content="The RAG index has not been built yet. Run `python -m jarvis.rag.setup` first.",
                success=False,
            )
        payload = [
            {"rank": i + 1, "score": h.score, "source": h.source, "heading": h.heading, "snippet": h.snippet}
            for i, h in enumerate(hits)
        ]
        content = json.dumps(payload, indent=2, ensure_ascii=False)
        return ToolResult(
            tool_name=self.tool_id,
            content=content if payload else "No matching chunks in the index.",
            success=True,
            metadata={"hits": len(payload)},
        )


__all__ = ["JarvisRagSearchTool"]
