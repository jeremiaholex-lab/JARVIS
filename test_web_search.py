"""web_search tool: offered as READ-tier, offline behavior pinned by monkeypatching."""
import json

import pytest

from jarvis.agent import tools as agent_tools
from jarvis.security.tiers import Tier


def test_web_search_tool_is_read_only_and_offered():
    available = agent_tools.build_tools()
    tool = next(t for t in available if t.name == "web_search")
    assert tool.tier is Tier.READ
    assert "web_search" in {s["function"]["name"] for s in agent_tools.tool_schemas(available)}


def test_web_search_returns_formatted_results(monkeypatch):
    # fetch_view's contract: a list of pre-formatted "title — snippet (domain)" strings.
    fake = ["Asyncio guide — An asyncio tutorial in 60 lines. (docs.python.org)"]
    import jarvis.skills.web_search as ws
    monkeypatch.setattr(ws, "WebSearchConnector", type("Fake", (), {"fetch_view": lambda self, q, limit=5, freshness="": fake}))
    out = json.loads(agent_tools.execute(agent_tools.build_tools(), "web_search", {"query": "python asyncio"}))
    assert out["results"] == fake


def test_web_search_error_becomes_json_error_string(monkeypatch):
    import jarvis.skills.web_search as ws
    monkeypatch.setattr(ws, "WebSearchConnector", type("Broken", (), {
        "fetch_view": lambda self, q, limit=5, freshness="": (_ for _ in ()).throw(
            RuntimeError("web search failed: RateLimit"))}))
    out = json.loads(agent_tools.execute(agent_tools.build_tools(), "web_search", {"query": "x"}))
    assert "error" in out
