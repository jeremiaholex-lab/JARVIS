"""Agent tools the orb conversation may use (design §6.7 read tier).

The orb used to be a chat box with no hands: projects, deadlines and indexed
documents existed in the runtime but were invisible to it. This module exposes
a *read-only* tool set to the model during an orb turn, and every tool is
checked through ``jarvis.security.tiers.classify`` before it is offered at all
— so the tier policy that the Settings panel displays is the same policy the
orb actually runs, not a mirror of the console's.

Writes stay where they belong: the console secured-ask path, with the
ConfirmGate and the audit chain. The orb deliberately has no write tool.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from jarvis.security.tiers import PlannedAction, Tier, classify

_logger = logging.getLogger(__name__)

# Names only. Execution is local and read-only; nothing here mutates state.
_READ_TOOL_NAMES = ("jarvis_projects_read", "jarvis_rag_search")


@dataclass(slots=True)
class AgentTool:
    name: str
    description: str
    parameters: Dict[str, Any]
    fn: Callable[..., str]
    tier: Tier


def _projects_read(view: str, project_name: str = "") -> str:
    import os

    # Default to *this repo's* root, not the cwd: the orb shell may launch the
    # server from orb/, and a cwd-based default would silently open (and create)
    # an empty database — the assistant would truthfully report "no deadlines"
    # that the owner never registered.
    root = Path(os.environ.get("JARVIS_WORKSPACE_ROOT") or Path(__file__).resolve().parents[2])
    db = Path(os.environ.get("JARVIS_PROJECT_DB") or root / "jarvis_projects.db")
    import sys

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from project_store import ProjectStore

    store = ProjectStore(db)
    if view == "projects":
        return json.dumps(store.list_projects(), ensure_ascii=False)
    if view == "due_soon":
        rows = store.get_proactive_deadlines()
        for r in rows:
            r.pop("created_at", None)
        return json.dumps(rows, ensure_ascii=False)
    if view == "deadlines":
        project = None
        if project_name:
            want = project_name.casefold()
            project = next((p for p in store.list_projects() if p["name"].casefold() == want), None)
            if project is None:
                return json.dumps({"error": f"unknown project {project_name!r}"})
        rows = store.list_deadlines(project["id"] if project else None, "open")
        for r in rows:
            p = store.get_project(r["project_id"])
            r["project_name"] = p["name"] if p else "Unknown"
        return json.dumps(rows, ensure_ascii=False)
    return json.dumps({"error": "view must be projects, deadlines, or due_soon"})


def _rag_search(query: str, top_k: int = 3) -> str:
    from jarvis.rag.setup import ask as rag_ask

    try:
        payload = rag_ask(query, top_k=max(1, min(int(top_k or 3), 5)))
    except FileNotFoundError:
        return json.dumps({"error": "document index not built yet — run: python phase4.py rag"})
    except Exception as exc:
        return json.dumps({"error": f"document search failed: {exc.__class__.__name__}"})
    if not payload["results"]:
        return json.dumps({"results": [], "note": "nothing in the index matches that"})
    # Keep the payload small: the voice answer needs the snippet, not stats.
    return json.dumps(
        {
            "results": [
                {"source": r["source"], "heading": r["heading"], "snippet": r["snippet"]}
                for r in payload["results"]
            ],
            "cite": "attribute any claim you use to its source path",
        },
        ensure_ascii=False,
    )


def _google_read(view: str, limit: int = 8) -> str:
    """Read only bounded Gmail metadata, today's calendar, or open tasks."""
    from jarvis.skills.google_daily import GoogleDailyConnector

    connector = GoogleDailyConnector()
    if not connector.client_file.is_file() or not connector.token_file.is_file():
        return json.dumps({"error": "Google is not connected; configure OAuth in JARVIS settings."})
    items = connector.fetch_view(view, limit=max(1, min(int(limit or 8), 20)))
    return json.dumps({"view": view, "items": items}, ensure_ascii=False)


def _microsoft_read(view: str, limit: int = 8) -> str:
    """Read bounded summaries from connected Microsoft 365 mail, calendar, or tasks."""
    from jarvis.skills.microsoft_365 import Microsoft365Connector

    connector = Microsoft365Connector()
    items = connector.fetch_view(view, limit=max(1, min(int(limit or 8), 20)))
    return json.dumps({"platform": "Microsoft 365", "view": view, "items": items}, ensure_ascii=False)


def _current_time() -> str:
    now = datetime.now().astimezone()
    return json.dumps({"iso": now.isoformat(timespec="seconds"), "weekday": now.strftime("%A")})


# The vision path needs live engine/settings objects, which module-level tool
# functions cannot see. The host injects a provider at startup.
_VISION_PROVIDER = None


def configure_vision(provider) -> None:
    """provider() -> (local_engine, cloud_factory, settings)"""
    global _VISION_PROVIDER
    _VISION_PROVIDER = provider


def _look_at_screen(question: str = "what is on this screen?") -> str:
    """§6.7 screen perception. Capture is local and free; the *reading* may
    not be, so consent is checked before a single pixel is sent anywhere."""
    from jarvis.vision.describe import describe_screen
    from jarvis.vision.screen import ScreenCapture, ScreenCaptureError

    if _VISION_PROVIDER is None:
        return json.dumps({"error": "vision is not wired up in this host"})
    if not ScreenCapture.available():
        return json.dumps({"error": "no screen capture backend on this host"})
    local_engine, cloud_factory, settings = _VISION_PROVIDER()
    try:
        screen = ScreenCapture(ttl=2.0).grab()
    except ScreenCaptureError as exc:
        return json.dumps({"error": f"screen capture failed: {exc}"})
    except Exception as exc:
        return json.dumps({"error": f"screen capture failed: {exc.__class__.__name__}"})
    try:
        result = describe_screen(screen, question, local_engine=local_engine,
                                 cloud_factory=cloud_factory, settings=settings)
    except Exception as exc:
        return json.dumps({"error": f"vision model failed: {exc.__class__.__name__}"})
    return json.dumps({
        "seen": result.text,
        "where": result.where,
        "model": result.model,
        "capture": f"{screen.width}x{screen.height} {result.ms}ms",
        "reason": result.reason,
    }, ensure_ascii=False)


def _check_tier(name: str, fn: Callable[..., str], description: str, parameters: Dict[str, Any]) -> Optional[AgentTool]:
    """Refuse to expose anything the tier table would not call READ."""
    probe = PlannedAction(tool=name, params={})
    tier = classify(probe)
    if tier is not Tier.READ:
        _logger.warning("orb tool %r is %s-tier — not exposed to the conversation", name, tier.value)
        return None
    return AgentTool(name=name, description=description, parameters=parameters, fn=fn, tier=tier)


def build_tools() -> List[AgentTool]:
    tools: List[AgentTool] = []
    projects = _check_tier(
        "jarvis_projects_read",
        _projects_read,
        "Read the user's registered projects and deadlines. view='projects' lists projects; "
        "view='due_soon' lists open deadlines inside the proactive window; "
        "view='deadlines' optionally filters by project_name.",
        {
            "type": "object",
            "properties": {
                "view": {"type": "string", "enum": ["projects", "deadlines", "due_soon"]},
                "project_name": {"type": "string"},
            },
            "required": ["view"],
        },
    )
    rag = _check_tier(
        "jarvis_rag_search",
        _rag_search,
        "Search the user's indexed documents (local embeddings over the folders they chose). "
        "Use when the answer may live in their own files. Returns cited snippets.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "description": "1-5, default 3"},
            },
            "required": ["query"],
        },
    )
    google = _check_tier(
        "google_read",
        _google_read,
        "Read limited metadata from the user's connected Google account. Choose view='email' "
        "for recent Gmail sender and subject only, view='calendar' for today's events, or "
        "view='tasks' for open Google Tasks. This tool is read-only and returns at most 20 items.",
        {
            "type": "object",
            "properties": {
                "view": {"type": "string", "enum": ["email", "calendar", "tasks"]},
                "limit": {"type": "integer", "description": "Maximum results (1-20, default 8)"},
            },
            "required": ["view"],
        },
    )
    microsoft = _check_tier(
        "microsoft365_read",
        _microsoft_read,
        "Read limited metadata from the user's connected Microsoft 365 account. Choose view='email' "
        "for recent Outlook sender/subject only, view='calendar' for today's events, or view='tasks' "
        "for Microsoft To Do tasks. Read-only; returns at most 20 items.",
        {
            "type": "object",
            "properties": {
                "view": {"type": "string", "enum": ["email", "calendar", "tasks"]},
                "limit": {"type": "integer", "description": "Maximum results (1-20, default 8)"},
            },
            "required": ["view"],
        },
    )
    clock = _check_tier(
        "get_current_time",
        _current_time,
        "Get the machine's current local date and time. Use for 'what time is it' and for "
        "reasoning about today/tomorrow instead of guessing.",
        {"type": "object", "properties": {}},
    )
    screen = _check_tier(
        "look_at_screen",
        _look_at_screen,
        "Look at the user's current screen and answer a question about it — an error "
        "message, what window is open, text on display. Reading stays on-device when a "
        "local vision model exists; otherwise it requires the user's vision consent and "
        "returns an explanation instead of sending anything.",
        {"type": "object", "properties": {"question": {"type": "string"}}},
    )
    tools.extend([t for t in (projects, rag, google, microsoft, clock, screen) if t is not None])
    return tools


def tool_schemas(tools: List[AgentTool]) -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
        }
        for t in tools
    ]


def execute(tools: List[AgentTool], name: str, arguments: Any) -> str:
    """Run one tool call. Anything unknown or non-READ becomes an error string."""
    tool = next((t for t in tools if t.name == name), None)
    if tool is None:
        return json.dumps({"error": f"tool {name!r} is not available in the orb (read-only tier)"})
    if classify(PlannedAction(tool=name, params={})) is not Tier.READ:
        return json.dumps({"error": f"tool {name!r} refused: not read-tier"})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    try:
        return tool.fn(**arguments)
    except TypeError as exc:
        return json.dumps({"error": f"bad arguments for {name}: {exc}"})
    except Exception as exc:  # tool failure must not kill the turn
        _logger.warning("orb tool %s failed: %s", name, exc)
        return json.dumps({"error": f"{name} failed: {exc.__class__.__name__}: {exc}"})


__all__ = ["AgentTool", "build_tools", "tool_schemas", "execute"]
