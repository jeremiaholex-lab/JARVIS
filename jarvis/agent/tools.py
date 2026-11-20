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
import sqlite3
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

# Non-READ tools execute() runs because their effect is local, reversible and
# actually happens (unlike a proposer). An explicit "remember this" belongs in
# the user model the user asked for; refusing it would lose the instruction.
_LOCAL_WRITE_TOOLS = ("jarvis_remember",)


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


_USER_MODEL = None


def configure_file_watchers(manager) -> None:
    """Inject the host's persisted, root-restricted file watcher manager."""
    global _FILE_WATCHERS
    _FILE_WATCHERS = manager


_SCHEDULES = None


def configure_schedules(store) -> None:
    """Inject the host's ScheduleStore (the cron layer over the task queue).

    Without a host, the schedule tool reports that it is not wired up
    instead of inventing a second store."""
    global _SCHEDULES
    _SCHEDULES = store


def _task_schedule(action: str, cron: str = "", instruction: str = "",
                   schedule_id: str = "") -> str:
    """Manage recurring cron schedules over the durable task queue."""
    from jarvis.agent.schedules import CronError

    if _SCHEDULES is None:
        return json.dumps({"error": "schedules are not wired up in this host"})
    try:
        if action == "create":
            sched = _SCHEDULES.create(cron, instruction)
            return json.dumps({
                "id": sched.id,
                "cron": sched.cron,
                "instruction": sched.instruction,
                "next_run_at": _fmt_ts(sched.next_run_at),
                "note": "Recurring; each slot enqueues one read-only background task.",
            }, ensure_ascii=False)
        if action == "list":
            return json.dumps({"schedules": [
                {"id": s.id, "cron": s.cron, "instruction": s.instruction,
                 "enabled": s.enabled, "next_run_at": _fmt_ts(s.next_run_at),
                 "last_fired_at": _fmt_ts(s.last_fired_at) if s.last_fired_at else "",
                 "last_task_id": s.last_task_id}
                for s in _SCHEDULES.list()
            ]}, ensure_ascii=False)
        if action in ("pause", "resume"):
            sched = _SCHEDULES.set_enabled(schedule_id, action == "resume")
            return json.dumps({"id": sched.id, "enabled": sched.enabled,
                               "next_run_at": _fmt_ts(sched.next_run_at)},
                              ensure_ascii=False)
        if action == "remove":
            _SCHEDULES.remove(schedule_id)
            return json.dumps({"id": schedule_id, "removed": True})
        return json.dumps({"error": "action must be create, list, pause, resume, or remove"})
    except CronError as exc:
        return json.dumps({"error": f"invalid cron expression: {exc}"}, ensure_ascii=False)
    except (KeyError, ValueError, OSError) as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


def _fmt_ts(ts: float) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") + " local"


def configure_user_model(model) -> None:
    """Inject the host's UserModel so jarvis_remember can pin a fact.

    Same shape as configure_writes: without a host, the tool reports that the
    user model is unavailable instead of writing to a second store.
    """
    global _USER_MODEL
    _USER_MODEL = model


def _remember(key: str = "", value: str = "") -> str:
    """Pin one durable fact about the user, at their request."""
    if _USER_MODEL is None:
        return json.dumps({"error": "the user model is not wired up in this host"})
    if not (value or "").strip():
        return json.dumps({"error": "no fact value provided"})
    try:
        fact = _USER_MODEL.put(key or value, value, source="explicit", pinned=True)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps({"remembered": fact.value, "key": fact.key, "pinned": True},
                      ensure_ascii=False)


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


def _task_enqueue(goal: str, idempotency_key: str = "") -> str:
    """Queue a bounded read-only background task for the orb task worker."""
    from jarvis.agent.task_runner import configured_task_store

    store = configured_task_store()
    if store is None:
        return json.dumps({"error": "durable task worker is not available in this host"})
    task = store.create(goal, idempotency_key=idempotency_key)
    return json.dumps({"task_id": task.id, "status": task.status,
                       "goal": task.goal, "note": "Queued; the task runs with read-only tools."},
                      ensure_ascii=False)


def _watch_files(action: str, watcher_id: str = "", root: str = "", pattern: str = "**/*",
                 task_instruction: str = "Summarize the changed file and its important points.") -> str:
    """Manage opt-in local file watchers; watcher tasks remain read-only."""
    manager = _FILE_WATCHERS
    if manager is None:
        return json.dumps({"error": "file watchers are not available in this host"})
    try:
        if action == "create":
            watcher = manager.create(root, pattern, task_instruction)
            return json.dumps({"watcher": {
                "id": watcher.id, "root": watcher.root, "pattern": watcher.pattern,
                "task_instruction": watcher.task_instruction, "enabled": watcher.enabled,
                "baseline": "Existing matching files were recorded; only later changes trigger tasks.",
            }}, ensure_ascii=False)
        if action == "list":
            return json.dumps({"watchers": [{
                "id": w.id, "root": w.root, "pattern": w.pattern,
                "task_instruction": w.task_instruction, "enabled": w.enabled,
                "last_triggered_at": w.last_triggered_at,
            } for w in manager.list()]}, ensure_ascii=False)
        if action in {"pause", "resume"}:
            watcher = manager.set_enabled(watcher_id, action == "resume")
            return json.dumps({"id": watcher.id, "enabled": watcher.enabled})
        if action == "remove":
            manager.remove(watcher_id)
            return json.dumps({"id": watcher_id, "removed": True})
        return json.dumps({"error": "action must be create, list, pause, resume, or remove"})
    except (KeyError, ValueError, OSError) as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


def _task_status(task_id: str = "", limit: int = 10) -> str:
    """Read durable background-task status without exposing local database internals."""
    from jarvis.agent.task_runner import configured_task_store

    store = configured_task_store()
    if store is None:
        return json.dumps({"error": "durable tasks are not available in this host"})
    if task_id:
        task = store.get(task_id)
        if task is None:
            return json.dumps({"error": "task not found"})
        return json.dumps({"task": {"id": task.id, "goal": task.goal, "status": task.status,
                                    "steps": task.steps, "result": task.result, "error": task.error},
                           "events": store.events(task_id)}, ensure_ascii=False)
    return json.dumps({"tasks": [{"id": t.id, "goal": t.goal, "status": t.status,
                                   "steps": t.steps, "result": t.result, "error": t.error}
                                  for t in store.list(limit=limit, include_terminal=False)]}, ensure_ascii=False)


def _plan_and_enqueue(goal: str, max_subtasks: int = 5, idempotency_key: str = "") -> str:
    """Decompose a goal into a plan of sub-tasks and enqueue them atomically."""
    from jarvis.agent.task_runner import configured_task_store
    from jarvis.agent.planner import decompose_goal

    store = configured_task_store()
    if store is None:
        return json.dumps({"error": "durable task worker is not available in this host"})

    try:
        plan = decompose_goal(goal, max_subtasks=max_subtasks, call_engine=_planner_engine,
                              idempotency_key=idempotency_key)
    except ValueError as exc:
        return json.dumps({"error": f"plan validation failed: {exc}"})
    except Exception as exc:
        return json.dumps({"error": f"planner unavailable: {exc.__class__.__name__}: {exc}"})

    try:
        root, subtasks = store.create_plan(plan)
    except sqlite3.IntegrityError:
        # A repeat of the same idempotency key: return the plan already queued
        # instead of surfacing a raw constraint error.
        existing = store.get_plan_root_by_key(f"{idempotency_key}:root") if idempotency_key else None
        if existing is None:
            return json.dumps({"error": "this plan was already queued"})
        subtasks = [t for t in store.get_plan_tasks(existing.id) if not t.is_plan_root]
        root = existing
    return json.dumps({
        "plan_id": root.id,
        "root_goal": root.goal,
        "subtasks": [
            {"id": t.id, "index": t.step_index, "goal": t.goal, "depends_on": t.depends_on,
             "role": t.role}
            for t in subtasks
        ],
        "note": f"Plan enqueued with {len(subtasks)} sub-tasks. Root task coordinates completion.",
    }, ensure_ascii=False)


def _planner_engine(engine: str, msgs: list, tools: list | None) -> dict:
    """Engine callable for the planner to use the local model.

    Raises rather than inventing a plan: a fabricated decomposition would be
    queued and executed as if the model had produced it.
    """
    from jarvis.agent.tools import _VISION_PROVIDER

    if _VISION_PROVIDER is None:
        raise ValueError("planner engine is not wired up in this host")

    local_engine, cloud_factory, settings = _VISION_PROVIDER()
    if local_engine is None:
        raise ValueError("planner engine is unavailable: no local model")

    from openjarvis.core.types import Message, Role
    role_map = {r.value: r for r in Role}
    converted = []
    for m in msgs:
        calls = [
            {"id": c.get("id", ""), "name": c.get("name", ""), "arguments": c.get("arguments", "{}")}
            for c in (m.get("tool_calls") or [])
        ]
        converted.append(Message(
            role=role_map.get(m.get("role", "user"), Role.USER),
            content=m.get("content") or "",
            tool_calls=calls or None,
            tool_call_id=m.get("tool_call_id"),
        ))
    return local_engine.generate(
        converted,
        model=settings["local_model"],
        num_ctx=4096,
        temperature=0.3,
        max_tokens=800,
    )


def _current_time() -> str:
    now = datetime.now().astimezone()
    return json.dumps({"iso": now.isoformat(timespec="seconds"), "weekday": now.strftime("%A")})


# The vision path needs live engine/settings objects, which module-level tool
# functions cannot see. The host injects a provider at startup.
_VISION_PROVIDER = None
_FILE_WATCHERS = None


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
    task_enqueue = _check_tier(
        "jarvis_task_enqueue",
        _task_enqueue,
        "Queue a bounded background task for JARVIS to complete using read-only tools. Do this "
        "only when the user explicitly asks to run something in the background or continue later. "
        "The task cannot send messages, write files, or make external changes; those stay in the "
        "existing confirmation flows.",
        {"type": "object", "properties": {
            "goal": {"type": "string", "description": "The user's requested task"},
            "idempotency_key": {"type": "string", "description": "Optional request id to prevent duplicate queueing"},
        }, "required": ["goal"]},
    )
    file_watch = _check_tier(
        "jarvis_file_watch",
        _watch_files,
        "Manage local file watchers. Create only when explicitly requested, and only under approved roots. "
        "Watchers enqueue bounded read-only tasks after a matching file stabilizes; they never edit files "
        "or send content externally. Use action=list to review, pause/resume to control, and remove to delete.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["create", "list", "pause", "resume", "remove"]},
            "root": {"type": "string", "description": "Existing approved folder to watch"},
            "pattern": {"type": "string", "description": "Relative glob such as **/*.md"},
            "task_instruction": {"type": "string", "description": "Read-only work to enqueue for each changed file"},
            "watcher_id": {"type": "string", "description": "Required for pause, resume, and remove"},
        }, "required": ["action"]},
    )
    task_status = _check_tier(
        "jarvis_task_status",
        _task_status,
        "Read status of queued JARVIS background tasks. Provide task_id for one task, or omit it "
        "to list recent active tasks. This tool cannot create, retry, or cancel tasks.",
        {"type": "object", "properties": {
            "task_id": {"type": "string"},
            "limit": {"type": "integer", "description": "1-20, default 10"},
        }},
    )
    task_schedule = _check_tier(
        "jarvis_task_schedule",
        _task_schedule,
        "Create or manage recurring scheduled tasks, using 5-field cron in local time "
        "(minute hour day-of-month month day-of-week; e.g. '0 9 * * 1' is every Monday 9am). "
        "Each slot enqueues one read-only background task; missed slots fire late once and "
        "never backfill. Use when the user wants work repeated on a schedule "
        "(e.g. 'every Monday, summarize last week's emails'). action=create needs cron and "
        "instruction; pause/resume/remove need schedule_id; list shows all schedules.",
        {"type": "object", "properties": {
            "action": {"type": "string",
                       "enum": ["create", "list", "pause", "resume", "remove"]},
            "cron": {"type": "string",
                     "description": "5-field cron expression, local time (create only)"},
            "instruction": {"type": "string",
                             "description": "What the scheduled task should do (create only)"},
            "schedule_id": {"type": "string",
                            "description": "Required for pause, resume, remove"},
        }, "required": ["action"]},
    )
    plan_and_enqueue = _check_tier(
        "jarvis_plan_and_enqueue",
        _plan_and_enqueue,
        "Decompose a goal into a plan of dependent sub-tasks and enqueue them atomically. "
        "Use when the user wants multi-step background work (research, summarization, analysis). "
        "The planner returns a DAG of sub-tasks with dependencies; the root task coordinates completion. "
        "Sub-tasks run with read-only tools and cannot perform writes.",
        {"type": "object", "properties": {
            "goal": {"type": "string", "description": "The user's high-level goal"},
            "max_subtasks": {"type": "integer", "description": "Maximum sub-tasks (1-10, default 5)"},
            "idempotency_key": {"type": "string", "description": "Optional request id to prevent duplicate queueing"},
        }, "required": ["goal"]},
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
                               files_search, notion, task_enqueue, task_status,
                               task_schedule, plan_and_enqueue,
                               file_watch, clock, screen) if t is not None])

    # Sandbox and browser automation. All six are READ-tier: the sandbox is
    # AST-validated and run inside the Docker/restricted-process boundary, and
    # the browser is a fresh headless session that writes nothing to the host.
    from jarvis.agent.sandbox_browser_tools import (
        browser_click, browser_extract, browser_navigate,
        browser_screenshot, browser_type, sandbox_execute,
    )
    sandbox = _check_tier(
        "sandbox_execute",
        sandbox_execute,
        "Run Python in an isolated sandbox and return what it prints. Use print() "
        "for results. Only json, math, and time may be imported; file IO, network, "
        "eval/exec, and private attributes are rejected before execution. Reports "
        "which isolation ran (docker or fallback) — do not claim container "
        "isolation when the note says fallback.",
        {"type": "object", "properties": {
            "code": {"type": "string", "description": "Python source to run"},
            "timeout": {"type": "integer", "description": "seconds, 1-60 (default 30)"},
        }, "required": ["code"]},
    )
    nav = _check_tier(
        "browser_navigate",
        browser_navigate,
        "Open a URL in a fresh headless browser and return the page title and text. "
        "Private, loopback, and cloud-metadata addresses are refused by SSRF "
        "protection. Use browser_extract for links or tables afterwards.",
        {"type": "object", "properties": {
            "url": {"type": "string"},
            "wait_for": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle"]},
        }, "required": ["url"]},
    )
    click = _check_tier(
        "browser_click",
        browser_click,
        "Click an element on the current browser page. Pass visible text, a CSS "
        "selector, or a short description like 'first video result'.",
        {"type": "object", "properties": {
            "selector": {"type": "string"},
            "by_text": {"type": "boolean", "description": "prefer text matching (default false)"},
        }, "required": ["selector"]},
    )
    type_ = _check_tier(
        "browser_type",
        browser_type,
        "Type text into a form field on the current browser page. clear=true "
        "replaces the field's contents (default); false appends.",
        {"type": "object", "properties": {
            "selector": {"type": "string"},
            "text": {"type": "string"},
            "clear": {"type": "boolean"},
        }, "required": ["selector", "text"]},
    )
    shot = _check_tier(
        "browser_screenshot",
        browser_screenshot,
        "Capture the current browser page and report its size. The image itself "
        "is not returned to the model; nothing is written to disk.",
        {"type": "object", "properties": {
            "full_page": {"type": "boolean", "description": "capture the full scrollable page"},
        }},
    )
    extract = _check_tier(
        "browser_extract",
        browser_extract,
        "Extract text, links, or tables from the current browser page. Run "
        "browser_navigate first.",
        {"type": "object", "properties": {
            "selector": {"type": "string", "description": "CSS selector (default 'body')"},
            "extract_type": {"type": "string", "enum": ["text", "links", "tables"]},
        }},
    )
    tools.extend([t for t in (sandbox, nav, click, type_, shot, extract) if t is not None])

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

    # Local, reversible, and something the user explicitly asked for. WRITE-tier
    # because it changes state, but execute() runs it directly (_LOCAL_WRITE_TOOLS):
    # nothing leaves the machine and there is no confirmation to wait for.
    tools.append(AgentTool(
        name="jarvis_remember",
        description=(
            "Pin one durable fact about the user when they ask you to remember "
            "something ('remember that I prefer short answers'). key is a short "
            "stable slug (e.g. 'answer-style'); value is one sentence. The fact "
            "is stored locally, is injected into future prompts, and survives "
            "restarts. Only call this when the user asks you to remember "
            "something, not for facts you inferred."
        ),
        parameters={
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "short stable slug, e.g. 'answer-style'"},
                "value": {"type": "string", "description": "one sentence to remember"},
            },
            "required": ["value"],
        },
        fn=_remember,
        tier=Tier.WRITE,
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
    if tier is not Tier.READ and name not in _PROPOSE_ONLY_TOOLS and name not in _LOCAL_WRITE_TOOLS:
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