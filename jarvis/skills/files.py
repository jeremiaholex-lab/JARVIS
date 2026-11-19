"""Read-only access to files on the user's own computer.

No new transport, no credentials: the laptop's filesystem, bounded and
filtered. Three operations, all READ-tier:

  read_file   a line-range of a text file
  list_dir    one level of a directory
  search      case-insensitive substring matches across a folder tree

Two guards that matter because orb answers can be escalated to the cloud
engine (Agnes) with the tool results attached:

  * secrets never flow through the orb. ``is_sensitive`` (the same
    heuristic the tier policy uses) rejects credential-shaped paths from
    being read or surfaced by search — an answer that contains ``.env``
    contents would leak them to the cloud on escalation.
  * binaries are refused, not truncated: feeding PNG/DB bytes to a
    language model is noise, and the error says so.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

from jarvis.security.tiers import is_sensitive

_MAX_READ_BYTES = 200_000     # read_file reads at most this much
_MAX_SEARCH_FILE_BYTES = 1_000_000
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".openjarvis", ".hg", ".svn"}


class LocalFiles:
    """Bounded read-only filesystem views for the agent's tools."""

    # ------------------------------------------------------------------ read

    def read_file(self, path: str, offset: int = 1, limit: int = 200) -> Dict:
        p = Path(path).expanduser()
        _refuse_sensitive(p)
        if not p.is_file():
            raise FileNotFoundError(f"no such file: {p}")
        data = p.read_bytes()
        truncated_bytes = len(data) > _MAX_READ_BYTES
        data = data[:_MAX_READ_BYTES]
        if b"\x00" in data:
            raise ValueError(f"{p.name} looks like a binary file; the orb reads text files only")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"{p.name} is not readable as UTF-8 text; the orb reads text files only")

        lines = text.splitlines()
        start = max(1, int(offset or 1))
        page = lines[start - 1:start - 1 + max(1, int(limit or 200))]
        return {
            "path": str(p),
            "total_lines": len(lines),
            "lines": f"{start}-{start - 1 + len(page)}",
            "text": "\n".join(page),
            "truncated_bytes": truncated_bytes,
        }

    # ----------------------------------------------------------------- list

    def list_dir(self, path: str, limit: int = 100) -> Dict:
        p = Path(path).expanduser()
        if not p.is_dir():
            raise NotADirectoryError(f"not a directory: {p}")
        entries: List[str] = []
        children = sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower()))
        for child in children:
            entries.append(child.name + ("/" if child.is_dir() else ""))
            if len(entries) >= int(limit or 100):
                break
        return {"path": str(p), "entries": entries, "shown": len(entries), "total": len(children)}

    # --------------------------------------------------------------- search

    def search(self, pattern: str, root: str, limit: int = 15) -> Dict:
        """Case-insensitive substring match, 'file:line: text' results."""
        root_p = Path(root).expanduser()
        if not root_p.is_dir():
            raise NotADirectoryError(f"not a directory: {root_p}")
        needle = (pattern or "").casefold()
        if not needle:
            raise ValueError("search needs a non-empty pattern")
        matches: List[str] = []
        count_limit = max(1, int(limit or 15))
        for dirpath, dirnames, filenames in _walk(root_p):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in filenames:
                fpath = Path(dirpath) / name
                if is_sensitive(str(fpath)):
                    continue
                try:
                    if fpath.stat().st_size > _MAX_SEARCH_FILE_BYTES:
                        continue
                    text = fpath.read_bytes().decode("utf-8")
                except (OSError, UnicodeDecodeError, ValueError):
                    continue  # binary or unreadable — not a text search target
                for i, line in enumerate(text.splitlines(), 1):
                    if needle in line.casefold():
                        matches.append(f"{fpath}:{i}: {line.strip()[:200]}")
                        if len(matches) >= count_limit:
                            return {"pattern": pattern, "root": str(root_p), "matches": matches, "more": True}
        return {"pattern": pattern, "root": str(root_p), "matches": matches, "more": False}


def _refuse_sensitive(p: Path) -> None:
    """Orb answers may reach the cloud engine, so secret-shaped paths are
    off-limits by name — the owner's .env, OAuth tokens, keys."""
    if is_sensitive(str(p)):
        raise PermissionError(
            f"{p.name} looks like a credential or secret file; the orb does not read "
            "those. Manage secrets in the console, not in a chat answer."
        )


def _walk(root: Path):
    import os

    for dirpath, dirnames, filenames in os.walk(root):
        yield dirpath, dirnames, filenames


__all__ = ["LocalFiles"]
