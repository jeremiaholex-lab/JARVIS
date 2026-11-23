"""Resolve a spoken app name to a launchable Windows target.

Pure matching logic plus a Windows Start-menu scan. Resolved targets are
shortcut (.lnk) files found in the Start menu; the launch policy in
``targets.py`` still applies before anything is opened.
"""
from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Tuple

__all__ = [
    "AppEntry",
    "Resolution",
    "resolve",
    "scan_shortcuts",
    "start_menu_dirs",
]

_MIN_CONTAINMENT = 3  # shortest query/name that may match by substring


@dataclass(frozen=True)
class AppEntry:
    name: str
    path: str


@dataclass(frozen=True)
class Resolution:
    status: str  # "match", "ambiguous", or "not_found"
    entry: Optional[AppEntry]
    candidates: Tuple[AppEntry, ...]
    reason: str


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").casefold()).strip()


def start_menu_dirs(env: Optional[Mapping[str, str]] = None) -> List[Path]:
    """Per-user and all-users Start-menu program folders."""
    env = os.environ if env is None else env
    dirs: List[Path] = []
    for var in ("APPDATA", "PROGRAMDATA"):
        base = env.get(var)
        if base:
            dirs.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return dirs


def scan_shortcuts(dirs: Iterable[Path], limit: int = 5000) -> List[AppEntry]:
    """Index Start-menu shortcuts by display name, skipping uninstallers."""
    found: dict = {}
    for root in dirs:
        if not root.is_dir():
            continue
        try:
            shortcuts = list(root.rglob("*.lnk"))
        except OSError:
            continue
        for path in shortcuts:
            name = path.stem
            key = _norm(name)
            if not key or "uninstall" in key or key in found:
                continue
            found[key] = AppEntry(name=name, path=str(path))
            if len(found) >= limit:
                break
    return sorted(found.values(), key=lambda e: e.name.casefold())


def resolve(
    query: str,
    index: Iterable[AppEntry],
    aliases: Optional[Mapping[str, str]] = None,
    cutoff: float = 0.6,
    margin: float = 0.08,
) -> Resolution:
    """Match a spoken name against the index.

    Order: exact name, then alias, then fuzzy/substring score. If the best
    two fuzzy scores are too close to call, the result is ``ambiguous`` and
    the caller should ask the user to choose from ``candidates``.
    """
    q = _norm(query)
    if not q:
        return Resolution("not_found", None, (), "No app name was given.")

    alias_map = {_norm(k): _norm(v) for k, v in (aliases or {}).items()}
    q = alias_map.get(q, q)

    entries = list(index)
    normed = [(_norm(e.name), e) for e in entries]

    exact = [e for n, e in normed if n == q]
    if exact:
        return Resolution("match", exact[0], tuple(exact[:5]), "Exact match.")

    scored: List[Tuple[float, AppEntry]] = []
    for n, e in normed:
        if not n:
            continue
        score = difflib.SequenceMatcher(None, q, n).ratio()
        if min(len(q), len(n)) >= _MIN_CONTAINMENT and (q in n or n in q):
            score = max(score, 0.85)
        if score >= cutoff:
            scored.append((score, e))

    if not scored:
        return Resolution("not_found", None, (), f"No installed app matches '{query}'.")

    scored.sort(key=lambda t: (-t[0], t[1].name.casefold()))
    best_score, best = scored[0]
    top = tuple(e for _, e in scored[:5])
    if len(scored) > 1 and best_score - scored[1][0] < margin and best_score < 0.99:
        return Resolution("ambiguous", None, top, f"Several apps match '{query}'; choose one.")
    return Resolution("match", best, top, "Closest match.")
