"""Turn a spoken app name into a launchable Start-menu shortcut.

Used before a confirmation card is created, so the approved payload is the
exact shortcut path the user is shown. Paths and file names pass through
unchanged and are judged by the launch policy in ``targets.py``.
"""
from __future__ import annotations

import difflib
import os
import re
import time
from typing import Iterable, Mapping, Optional

from jarvis.desktop.app_resolver import AppEntry, resolve, scan_shortcuts, start_menu_dirs
from jarvis.desktop.app_resolver import _norm as _resolver_norm

__all__ = ["DEFAULT_APP_ALIASES", "prepare_open_app", "cached_start_menu_index"]

# Common spoken names mapped to the name most likely used in the Start menu.
# Keys and values are matched case-insensitively after normalisation.
DEFAULT_APP_ALIASES: Mapping[str, str] = {
    "browser": "google chrome",
    "chrome": "google chrome",
    "edge": "microsoft edge",
    "code": "visual studio code",
    "vs code": "visual studio code",
    "vscode": "visual studio code",
    "word": "microsoft word",
    "excel": "microsoft excel",
    "powerpoint": "microsoft powerpoint",
    "notes": "notepad",
}

_INDEX_TTL_SECONDS = 600.0
_index_cache: Optional[list] = None
_index_at = 0.0

_PATHLIKE = re.compile(r"[\\/:]|\.[A-Za-z0-9]{1,5}$")


def _suggestions(text: str, entries: list, limit: int = 3) -> list:
    """Closest installed app names for a query that matched nothing.

    Deliberately looser than ``resolve``'s cutoff: a miss should still say
    what *is* installed, so the user (or the model) can correct the name in
    one round trip instead of a blind retry. Names only — never paths, so a
    suggestion cannot smuggle a launchable target through an error message.
    """
    q = _resolver_norm(text)
    if not q:
        return []
    scored = []
    for entry in entries:
        n = _resolver_norm(entry.name)
        if not n:
            continue
        score = difflib.SequenceMatcher(None, q, n).ratio()
        if min(len(q), len(n)) >= 3 and (q in n or n in q):
            score = max(score, 0.85)
        if score > 0.3:
            scored.append((score, entry.name))
    scored.sort(key=lambda t: (-t[0], t[1].casefold()))
    return [name for _, name in scored[:limit]]


def cached_start_menu_index(now: Optional[float] = None) -> list:
    """Start-menu shortcut index, rescanned at most every ten minutes."""
    global _index_cache, _index_at
    now = time.monotonic() if now is None else now
    if _index_cache is None or now - _index_at > _INDEX_TTL_SECONDS:
        _index_cache = scan_shortcuts(start_menu_dirs(os.environ))
        _index_at = now
    return _index_cache


def prepare_open_app(
    name: str,
    index: Optional[Iterable[AppEntry]] = None,
    aliases: Optional[Mapping[str, str]] = None,
) -> dict:
    """Resolve a spoken app name.

    Returns one of:
      * ``{"ok": True, "path": None, "name": None}`` - the input looks like a
        path or file name; the caller applies the launch policy to it as-is.
      * ``{"ok": True, "path": <shortcut>, "name": <display name>}``
      * ``{"ok": False, "error": ..., "candidates": [...]}`` - nothing safe to
        propose; the caller must not create a card.
    """
    text = str(name or "").strip()
    if not text:
        return {"ok": False, "error": "No app name was given.", "candidates": []}
    if _PATHLIKE.search(text):
        return {"ok": True, "path": None, "name": None}

    entries = list(index) if index is not None else cached_start_menu_index()
    result = resolve(text, entries,
                     aliases=DEFAULT_APP_ALIASES if aliases is None else aliases)
    if result.status == "match" and result.entry is not None:
        return {"ok": True, "path": result.entry.path, "name": result.entry.name}
    if result.status == "ambiguous":
        return {
            "ok": False,
            "error": f"Several installed apps match '{text}'. Say the full name.",
            "candidates": [e.name for e in result.candidates],
        }
    suggestions = _suggestions(text, list(entries))
    hint = ""
    if suggestions:
        hint = " Did you mean: " + ", ".join(suggestions) + "?"
    return {
        "ok": False,
        "error": f"No installed app matches '{text}'. "
                 f"Try the name as it appears in the Start menu.{hint}",
        "candidates": suggestions,
    }
