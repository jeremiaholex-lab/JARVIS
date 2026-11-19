"""Read-only DuckDuckGo web search — no API key, no new account.

The free endpoint keeps general-questions answering off the escalation
budget: the local model gets titles/links/snippets it can speak or cite.
If the endpoint is rate-limited or blocked, the tool returns an error
string so the turn can fall back to the cloud escalation path.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import List

_MAX_RESULTS = 5


class WebSearchConnector:
    def fetch_view(self, query: str, limit: int = _MAX_RESULTS, freshness: str = "") -> list[str]:
        limit = max(1, min(int(limit or _MAX_RESULTS), _MAX_RESULTS))
        try:
            from duckduckgo_search import DDGS
        except ImportError as exc:
            raise RuntimeError("web search needs the ddgs package; install project dependencies.") from exc
        try:
            results = DDGS().text(query, max_results=limit, safesearch="moderate",
                                   timelimit=freshness or None)
        except Exception as exc:
            raise RuntimeError(f"web search failed: {exc.__class__.__name__}") from exc
        if not results:
            return []
        return [
            f"{r.get('title') or '(untitled)'} — {_shorten(r.get('body', ''), 220)} "
            f"({urllib.parse.urlparse(r.get('href', '')).netloc})"
            for r in results
        ]


def _shorten(text: str, cap: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"
