"""notion_read tool: offered as READ-tier, offline behavior pinned by monkeypatching."""
import json

from jarvis.agent import tools as agent_tools
from jarvis.security.tiers import Tier


def test_notion_tool_is_read_only_and_offered():
    available = agent_tools.build_tools()
    tool = next(t for t in available if t.name == "notion_read")
    assert tool.tier is Tier.READ
    assert "notion_read" in {s["function"]["name"] for s in agent_tools.tool_schemas(available)}


def test_notion_search_returns_formatted_items(monkeypatch):
    # fetch_view's contract: a list of pre-formatted "title [type] url" strings.
    fake = ["Meeting notes [page] https://notion.so/x"]
    import jarvis.skills.notion as ns
    monkeypatch.setattr(ns, "NotionConnector", type("Fake", (), {
        "fetch_view": lambda self, view, limit=8, query="": fake}))
    out = json.loads(agent_tools.execute(agent_tools.build_tools(), "notion_read",
                                         {"view": "search", "query": "meeting"}))
    assert out["items"] == fake


def test_notion_bad_view_becomes_json_error(monkeypatch):
    import jarvis.skills.notion as ns
    monkeypatch.setattr(ns, "NotionConnector", type("Broken", (), {
        "fetch_view": lambda self, view, limit=8, query="": (_ for _ in ()).throw(
            ValueError("view must be search, note, or databases"))}))
    out = json.loads(agent_tools.execute(agent_tools.build_tools(), "notion_read", {"view": "bogus"}))
    assert "error" in out


def test_notion_block_text_flattens_rich_text_and_children():
    from jarvis.skills.notion import NotionConnector

    assert NotionConnector._block_text({"type": "paragraph", "paragraph": {"rich_text": [
        {"plain_text": "hello"}, {"plain_text": " world"}]}}) == "hello world"
    assert NotionConnector._block_text({"type": "toggle", "toggle": {"sub_blocks": [
        {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "child"}]}}]}}) == "child"


def test_notion_page_title_handles_page_and_database():
    from jarvis.skills.notion import NotionConnector

    page = {"object": "page", "properties": {"title": {"rich_text": [{"plain_text": "My Note"}]}}}
    database = {"object": "database", "title": [{"plain_text": "Projects"}]}
    assert NotionConnector._page_title(page) == "My Note"
    assert NotionConnector._page_title(database) == "Projects"
