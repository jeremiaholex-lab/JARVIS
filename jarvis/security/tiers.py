"""Permission tiers (design §6.7) — classify a planned tool call into one of:

  READ         free, no prompt
  WRITE        one-tap confirm, reversible (undo record kept)
  DESTRUCTIVE  hard gate: explicit typed confirmation, never batched

The classifier is deterministic (a table, not a model) so the tiering itself
never flakes; the *auditor* (auditor.py) is the LLM critic that can still
escalate a READ/WRITE call to HOLD before the user is prompted.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping

# Tool names OpenJarvis marks with spec.requires_confirmation. Only these
# reach the confirm gate; everything else is structurally free.
_CONFIRMED_TOOL_TIERS: Dict[str, str] = {
    "file_write": "write",
    "file_write_confirmed": "write",
    "shell_exec": "write",
    "jarvis_projects_write": "write",
    # Stage 3: editing source files produces executable text; a one-tap
    # confirm is too cheap a gate for that, so it takes the typed phrase.
    "jarvis_file_edit": "destructive",
    "git_commit": "write",
    "calendar_create": "write",
    "task_create": "write",
    "gdrive_write": "write",
    "gdrive_create": "write",
    "gdrive_delete": "destructive",
    "git_push": "destructive",
    "file_delete": "destructive",
    "browser_post": "destructive",
}

# Deletion/destruction markers in a shell command string.
_DESTRUCTIVE_SHELL = re.compile(
    r"""(?xi)
    \b(rm\s+(?:-\w+\s+)*(--recursive\s+|-r\s+)?-\w*f)          # rm -rf
  | \b(rm\s+-\w*rf\w*)
  | \b(shred\s+)
  | \b(unlink\s+)
  | \b(git\s+reset\s+(?:--hard|--merge)\b)
  | \b(git\s+clean\s+(?:-\w+\s+)*-[a-z]*f)
  | \b(git\s+push\s+.*--force)
  | \b(format\s+c:)                                             # disk format
  | \b(del\s+/s\b)
  | \b(rd\s+/s\b)
  | \b(mkfs\w*)
  | \b(drop\s+table\b)
  | \b(truncate\s+table\b)
  """
)

# Shell writes that are destructive to *state* even without a verb match
# (overwriting env, changing permissions of system files, publishing).
_HIGH_RISK_SHELL = re.compile(
    r"(?i)\b(chmod\s+777\b|curl.*-d\b.*\bpost\b|gh\s+(pr|issue|repo)\s+create|git\s+push\s+(?!.*--dry-run))"
)


class Tier(str, Enum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


@dataclass(slots=True)
class PlannedAction:
    """One planned agent tool call, fully specified before execution."""

    tool: str
    params: Mapping[str, Any] = field(default_factory=dict)

    @property
    def signature(self) -> str:
        """Stable identity of this action (used by the anti-batching check)."""
        return json.dumps(
            {"tool": self.tool, "params": dict(self.params)},
            sort_keys=True,
        )


def _command_blob(params: Mapping[str, Any]) -> str:
    return " ".join(str(v) for v in params.values() if isinstance(v, str))


def classify(action: PlannedAction) -> Tier:
    """Deterministic tier for a planned action."""
    tool = action.tool.casefold()

    # 1. Explicit destructive markers on a known tool.
    if _CONFIRMED_TOOL_TIERS.get(tool, "write") == "destructive":
        return Tier.DESTRUCTIVE

    # 2. Shell execution — inspect the command text.
    if tool in {"shell_exec", "docker_shell_exec"}:
        cmd = _command_blob(action.params)
        if _DESTRUCTIVE_SHELL.search(cmd):
            return Tier.DESTRUCTIVE
        if _HIGH_RISK_SHELL.search(cmd):
            return Tier.DESTRUCTIVE
        # Any shell that is not explicitly a read-only probe is a write.
        return Tier.WRITE

    # 3. Reads are free.
    if tool in {
        "file_read",
        "file_read_confirmed",
        "jarvis_projects_read",
        "gdrive_read",
        "gdrive_search",
        "calendar_list",
        "task_list",
        "task_get",
        "browser_get",
        "get_current_time",
        "look_at_screen",  # §6.7: looking at the screen is a read-only action
        "grep",
        "glob",
        "ls",
        "cat",
        "jarvis_rag_search",
        "google_read",
        "web_search",
        "notion_read",
        "read_file",
        "list_dir",
        "search_files",
        "jarvis_task_status",
        "jarvis_task_enqueue",
        "jarvis_file_watch",
        # Plan enqueue only writes read-only sub-tasks into the local queue; it
        # touches nothing on the host. Without this entry the planner tool is
        # classified WRITE and _check_tier silently drops it from the tool list.
        "jarvis_plan_and_enqueue",
        # Recurring schedule configuration: writes only to the local SQLite
        # schedule table and enqueues read-only queue rows; no host mutation.
        "jarvis_task_schedule",
        # Sandboxed computation: AST-validated, no file IO, no network, and
        # run inside the Docker/restricted-process sandbox. The code cannot
        # touch the host; the sandbox is the containment, not this table.
        "sandbox_execute",
        # Browser tools drive a fresh headless Chromium with no saved logins
        # and no host filesystem writes; navigate/extract/screenshot are pure
        # reads, click/type act only on the remote page.
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_screenshot",
        "browser_extract",
    }:
        return Tier.READ

    # 4. Anything else that reached the gate defaults to WRITE (conservative).
    return Tier.WRITE


# ---------------------------------------------------------------------------
# Anti-batching: destructive actions are never batched with other actions.
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class BatchCheck:
    ok: bool
    reason: str = ""


def check_batched(actions: list[PlannedAction], batch: list[PlannedAction]) -> BatchCheck:
    """§6.7: a destructive action must be the ONLY one in its batch.

    `actions` is the full sequence of planned calls this turn; `batch` is the
    contiguous slice that would execute together.
    """
    de = [a for a in batch if classify(a) is Tier.DESTRUCTIVE]
    if not de:
        return BatchCheck(True)
    others = [a for a in batch if a.signature not in {d.signature for d in de}]
    if others:
        return BatchCheck(
            False,
            f"destructive action cannot be batched with {len(others)} other action(s); "
            "it must run alone",
        )
    return BatchCheck(True)


def is_sensitive(path_like: str) -> bool:
    """Heuristic used by both tiering and the auditor: does this touch secrets?"""
    p = str(path_like).casefold()
    markers = (
        ".env", "secret", "credential", "password", "api_key", "apikey",
        "token", ".pem", ".key", "id_rsa", "google-oauth-client", "oauth",
        "private_key", "jwt",
    )
    return any(m in p for m in markers)


__all__ = [
    "Tier",
    "PlannedAction",
    "BatchCheck",
    "classify",
    "check_batched",
    "is_sensitive",
]
