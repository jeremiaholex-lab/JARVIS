"""Agent tools the orb conversation may use (design §6.7 read tier).

The orb exposes a *read-only* tool set to the model during a turn, and every
read tool is checked through ``jarvis.security.tiers.classify`` before it is
offered at all — so the tier policy the Settings panel displays is the same
policy the orb actually runs, not a mirror of the console's.

Stage 2 adds one WRITE-tier tool, ``jarvis_projects_write``: the model may
*propose* a write to the local project/deadline store, but the tool call is
side-effect free — it only registers a pending write and returns a
confirmation token. The mutation runs later through the host's confirm path
(one-tap for WRITE, typed phrase for DESTRUCTIVE) into the same hash-chained
AuditLogger the console ConfirmGate uses. ``execute()`` still refuses every
real mutation: the only non-READ tools it will run are the safe proposers.

Stage 3 adds ``jarvis_file_edit`` on the same pattern but a DESTRUCTIVE tier:
it proposes an edit to one text file, returns a unified diff, and the edit
runs only when the user types the confirmation phrase. The gate is enforced
in the executor, not the UI.
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

# Non-READ tools execute() will still run, because their fn is a side-effect
# free *proposer*: it only queues a pending change for host confirmation. The
# list must stay tiny and explicit — a new write tool is not callable here
# until it is added to this tuple and proven to do no mutation on its own.
_PROPOSE_ONLY_TOOLS = ("jarvis_projects_write", "jarvis_file_edit")


@dataclass(slots=True)
class AgentTool:
    name: str
    description: str
    parameters: Dict[str, Any]
    fn: Callable[..., str]
    tier: Tier


def _project_store():
    """The same local SQLite store the read tool opens, so a write lands where
    the assistant just read from. Callers get a ProjectStore, not a path."""
    import os

    root = Path(os.environ.get("JARVIS_WORKSPACE_ROOT") or Path(__file__).resolve().parents[2])
    db = Path(os.environ.get("JARVIS_PROJECT_DB") or root / "jarvis_projects.db")
    import sys

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from project_store import ProjectStore

    return ProjectStore(db)


def _projects_read(view: str, project_name: str = "") -> str:
    # Default to *this repo's* root, not the cwd: the orb shell may launch the
    # server from orb/, and a cwd-based default would silently open (and create)
    # an empty database — the assistant would truthfully report "no deadlines"
    # that the owner never registered.
    store = _project_store()
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


def _web_search(query: str, limit: int = 5, freshness: str = "") -> str:
    """Read-only DuckDuckGo search; no API key or account required."""
    from jarvis.skills.web_search import WebSearchConnector

    try:
        items = WebSearchConnector().fetch_view(query, limit=limit, freshness=freshness)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    return json.dumps({"query": query, "results": items,
                       "cite": "attribute any claim you use to its domain"}, ensure_ascii=False)


def _read_file(path: str, offset: int = 1, limit: int = 200) -> str:
    """Read one text file (bounded), with the secret-file guard applied."""
    from jarvis.skills.files import LocalFiles

    try:
        return json.dumps(LocalFiles().read_file(path, offset=offset, limit=limit), ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": f"{exc.__class__.__name__}: {exc}"}, ensure_ascii=False)


def _list_dir(path: str, limit: int = 100) -> str:
    """One level of a directory: entry names, dirs marked with a slash."""
    from jarvis.skills.files import LocalFiles

    try:
        return json.dumps(LocalFiles().list_dir(path, limit=limit), ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": f"{exc.__class__.__name__}: {exc}"}, ensure_ascii=False)


def _search_files(pattern: str, root: str, limit: int = 15) -> str:
    """Case-insensitive substring match across a folder's text files."""
    from jarvis.skills.files import LocalFiles

    try:
        return json.dumps(LocalFiles().search(pattern, root, limit=limit), ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": f"{exc.__class__.__name__}: {exc}"}, ensure_ascii=False)


def _notion_read(view: str = "search", query: str = "", limit: int = 8) -> str:
    """Read-only Notion notes; needs JARVIS_NOTION_TOKEN and pages shared with it."""
    from jarvis.skills.notion import NotionConnector

    connector = NotionConnector()
    try:
        items = connector.fetch_view(view, limit=limit, query=query)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    return json.dumps({"view": view, "items": items}, ensure_ascii=False)


# The write surface (stage 2): a WRITE-tier tool the model may *propose*. Its
# function is side-effect free — it registers a pending write and returns a
# confirmation token; the mutation runs later in SecuredWrites.resolve(),
# only after the host surfaces the token and the user confirms.
_WRITE_EXECUTOR = None
_FILE_EDITOR = None


def configure_writes(executor) -> None:
    """executor is a SecuredWrites. Until a host injects one, the write tool
    reports that writes are not wired in here (the console does this; the
    telegram host will too)."""
    global _WRITE_EXECUTOR
    _WRITE_EXECUTOR = executor


def configure_file_edits(editor) -> None:
    """editor is a SecuredFileEdits. Same contract as configure_writes: no
    host wiring means the proposer reports that edits are unavailable."""
    global _FILE_EDITOR
    _FILE_EDITOR = editor


def _projects_write(action: str = "", name: str = "", path: str = "",
                    project_name: str = "", title: str = "", due_date: str = "",
                    deadline_id=None, status: str = "") -> str:
    """Propose one write to the local project/deadline store (never executes)."""
    if _WRITE_EXECUTOR is None:
        return json.dumps({"error": "writes are not wired up in this host"})
    params: Dict[str, Any] = {"action": action}
    for key, val in (("name", name), ("path", path), ("project_name", project_name),
                     ("title", title), ("due_date", due_date),
                     ("deadline_id", deadline_id), ("status", status)):
        if val not in (None, ""):
            params[key] = val
    out = _WRITE_EXECUTOR.propose(params)
    return json.dumps(out, ensure_ascii=False)


def _file_edit(action: str = "", path: str = "", old_string: str = "",
               new_string: str = "", content: str = "") -> str:
    """Propose one edit to a text file (never executes). Returns the diff."""
    if _FILE_EDITOR is None:
        return json.dumps({"error": "file edits are not wired up in this host"})
    params: Dict[str, Any] = {"action": action, "path": path}
    if old_string:
        params["old_string"] = old_string
    if new_string:
        params["new_string"] = new_string
    if content:
        params["content"] = content
    out = _FILE_EDITOR.propose(params)
    return json.dumps(out, ensure_ascii=False)


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
    web = _check_tier(
        "web_search",
        _web_search,
        "Search the web (DuckDuckGo, no account needed). Use for general questions "
        "whose answer is not in the user's own data. Returns titles, snippets, and "
        "domains; attribute any claim to its source.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "description": "1-5, default 5"},
                "freshness": {"type": "string", "description": "optional timelimit: d (day), w (week), m (month), y (year)"},
            },
            "required": ["query"],
        },
    )
    files_read = _check_tier(
        "read_file",
        _read_file,
        "Read a text file on the user's computer by path (a line range: offset/limit). "
        "Secret-shaped files (.env, tokens, keys) are refused by name. Use when the "
        "answer lives in one specific file rather than the indexed document library.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "description": "first line, 1-based"},
                "limit": {"type": "integer", "description": "max lines, default 200"},
            },
            "required": ["path"],
        },
    )
    files_list = _check_tier(
        "list_dir",
        _list_dir,
        "List one level of a directory on the user's computer (names only, dirs "
        "marked with a slash). Use to find the path to pass to read_file or search_files.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer", "description": "max entries, default 100"},
            },
            "required": ["path"],
        },
    )
    files_search = _check_tier(
        "search_files",
        _search_files,
        "Search text files under a folder for a substring (case-insensitive), "
        "returning 'path:line: text' matches. Skips binary files and secret-shaped "
        "files. Use when you do not know which file holds the answer.",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "root": {"type": "string", "description": "folder to search under"},
                "limit": {"type": "integer", "description": "max matches, default 15"},
            },
            "required": ["pattern", "root"],
        },
    )
    notion = _check_tier(
        "notion_read",
        _notion_read,
        "Read the user's Notion notes. view='search' lists matching pages/databases "
        "(needs query); view='note' returns the readable body of the best-matching page "
        "(needs query); view='databases' lists shared database names. Use when the "
        "answer may live in their notes. Returns cited titles/URLs or body text.",
        {
            "type": "object",
            "properties": {
                "view": {"type": "string", "enum": ["search", "note", "databases"]},
                "query": {"type": "string"},
                "limit": {"type": "integer", "description": "1-20, default 8"},
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
    tools.extend([t for t in (projects, rag, google, web, files_read, files_list,
                               files_search, notion, clock, screen) if t is not None])

    # Stage 2: one WRITE-tier tool. It is advertised (so the model knows it can
    # act), but its fn is a side-effect-free proposer — running it only queues
    # a pending write. It is not built through _check_tier, which is READ-only.
    tools.append(AgentTool(
        name="jarvis_projects_write",
        description=(
            "Propose one write to the user's local projects/deadlines store. "
            "action='add_project' (needs name, path); action='add_deadline' "
            "(needs project_name, title, due_date); action='set_deadline_status' "
            "(needs deadline_id, status open/completed/cancelled). This only "
            "queues the change: it returns a confirmation token and a summary, "
            "and NOTHING is saved until the user confirms it in the UI. Always "
            "tell the user the write is waiting on their confirmation."),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add_project", "add_deadline", "set_deadline_status"]},
                "name": {"type": "string"},
                "path": {"type": "string"},
                "project_name": {"type": "string"},
                "title": {"type": "string"},
                "due_date": {"type": "string", "description": "ISO date like 2026-12-01"},
                "deadline_id": {"type": "integer"},
                "status": {"type": "string", "enum": ["open", "completed", "cancelled"]},
            },
            "required": ["action"],
        },
        fn=_projects_write,
        tier=Tier.WRITE,
    ))

    # Stage 3: a DESTRUCTIVE-tier file-edit proposer. Advertised so the model
    # can propose a concrete change, but running it only computes a diff and
    # queues it — the file is untouched until the user types the phrase.
    tools.append(AgentTool(
        name="jarvis_file_edit",
        description=(
            "Propose an edit to ONE text file on the user's computer. Read the file "
            "with read_file first and copy old_string exactly from it. "
            "action='replace' (needs old_string, new_string: old_string must appear "
            "exactly once — include surrounding lines for uniqueness); "
            "action='rewrite' (needs content: whole new file body); "
            "action='create' (needs content: a new file; never overwrites). "
            "This only computes a diff and returns a token — NOTHING is written. "
            "The user must type a confirmation phrase in the UI before it applies. "
            "Show them the diff and say it is waiting on their confirmation."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["replace", "rewrite", "create"]},
                "path": {"type": "string", "description": "absolute path to the file"},
                "old_string": {"type": "string", "description": "exact existing text (replace)"},
                "new_string": {"type": "string", "description": "replacement text (replace)"},
                "content": {"type": "string", "description": "full body (rewrite/create)"},
            },
            "required": ["action", "path"],
        },
        fn=_file_edit,
        tier=Tier.DESTRUCTIVE,
    ))
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
    """Run one tool call. Reads run free; only the safe *proposer* below runs
    for writes. Any other non-READ tool becomes an error string — a real
    mutation never happens here, it waits in a pending write for confirmation."""
    tool = next((t for t in tools if t.name == name), None)
    if tool is None:
        return json.dumps({"error": f"tool {name!r} is not available in the orb (read-only tier)"})
    tier = classify(PlannedAction(tool=name, params={}))
    if tier is not Tier.READ and name not in _PROPOSE_ONLY_TOOLS:
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
