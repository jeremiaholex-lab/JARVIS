"""Read-only DuckDuckGo web search - no API key, no new account.

The free endpoint keeps general-questions answering off the escalation
budget: the brain gets titles/links/snippets it can speak or cite. If the
endpoint is rate-limited or blocked, the tool returns an error string so the
turn can fall back to the cloud escalation path.

The package behind this was renamed: it installs as ``ddgs`` now and used to
install as ``duckduckgo_search``. Both import names are accepted, so the tool
works on a fresh runtime install and on an older checkout alike - the rename
alone must never look like "web search is not installed".

The free endpoint also answers "no results" for a live query roughly a third
of the time, and the same query succeeds a moment later on a different
backend. One attempt therefore made a working tool report itself broken, so
backends are rotated (measured 4/4 vs 3/4 and 2/4 on this machine) and a
thoroughly empty answer is returned as "no results" rather than an error.
"""
from __future__ import annotations

import html
import re
import urllib.parse
from typing import Any, Callable, Dict, List

_MAX_RESULTS = 5
# Rotated on until something answers. "auto" is the package default; "lite"
# and "html" are independent endpoints behind the same query.
_BACKENDS = ("auto", "lite", "html")
# News first for a time-bounded query (real articles, sources and dates),
# reference search first otherwise. The text endpoint answers "top headlines"
# with news HOME PAGES, which is how a searching turn still ended up with
# nothing to cite; the news endpoint answers with the stories themselves.
_METHODS = {"news": ("news", "text"), "web": ("text", "news")}
_MAX_ATTEMPTS = 4
_TAG = re.compile(r"<[^>]+>")
# Spoken answers must not inherit the mojibake DuckDuckGo sometimes returns.
_JUNK = re.compile(r"[\ufffd\u200b-\u200f\ufeff]")

# "day"/"week"/... as a model may guess them, to the one letter the API takes.
_FRESHNESS = {
    "d": "d", "day": "d", "today": "d", "daily": "d", "24h": "d", "24 hours": "d",
    "w": "w", "week": "w", "weekly": "w", "this week": "w", "7d": "w",
    "m": "m", "month": "m", "monthly": "m", "30d": "m",
    "y": "y", "year": "y", "yearly": "y",
}
_VALID_FRESHNESS = {"d", "w", "m", "y"}


class WebSearchConnector:
    def fetch_view(self, query: str, limit: int = _MAX_RESULTS, freshness: str = "",
                   kind: str = "auto") -> List[str]:
        """Search the web. Returns one line per result, or [] if nothing was found.

        ``freshness`` is d/w/m/y; giving it (or ``kind="news"``) searches DuckDuckGo
        News, which returns dated articles rather than homepages.
        """
        query = (query or "").strip()
        if not query:
            return []
        limit = max(1, min(int(limit or _MAX_RESULTS), _MAX_RESULTS))
        ddgs = _ddgs_client()
        base: Dict[str, Any] = {"max_results": limit, "safesearch": "moderate"}
        window = _freshness(freshness)
        if window:
            base["timelimit"] = window

        wanted = str(kind or "auto").strip().lower()
        mode = wanted if wanted in _METHODS else ("news" if window else "web")

        results: List[Any] = []
        errors: List[Exception] = []
        for method in _METHODS[mode]:
            for backend in _BACKENDS:
                client = ddgs()
                call = getattr(client, method, None)
                if call is None:
                    errors.append(RuntimeError("no results found"))
                    break
                kwargs = dict(base) if backend == "auto" else {**base, "backend": backend}
                try:
                    results = call(query, **kwargs) or []
                    break
                except TypeError:
                    # A package version that has no backend= argument at all.
                    try:
                        results = call(query, **base) or []
                    except Exception as exc:
                        errors.append(exc)
                    break
                except Exception as exc:
                    errors.append(exc)
            if results:
                break

        if not results:
            if errors and all(_is_no_results(e) for e in errors):
                return []           # every endpoint answered: nothing to find
            kind_name = errors[-1].__class__.__name__ if errors else "unknown error"
            # Name the failure the way it actually reads: endpoint trouble,
            # never a missing dependency.
            raise RuntimeError(
                f"web search endpoint unavailable ({kind_name}); try again shortly")
        return [_line(r) for r in results if isinstance(r, dict)]


def _is_no_results(exc: BaseException) -> bool:
    return "no results" in str(exc).lower()


def _ddgs_client() -> Callable[[], Any]:
    """Return the DDGS class from whichever package name is installed."""
    try:
        from ddgs import DDGS
        return DDGS
    except ImportError:
        pass
    try:
        from duckduckgo_search import DDGS      # retired name, still installed
        return DDGS
    except ImportError as exc:
        raise RuntimeError(
            "web search is not installed in the environment running JARVIS "
            "(needs the `ddgs` package)") from exc


def _freshness(value: str) -> str:
    """Normalise a caller's freshness guess to d/w/m/y, or '' for no filter.

    An unrecognised word is dropped rather than passed through: a ValueError
    from the API would otherwise read to the user as a broken tool.
    """
    key = str(value or "").strip().lower()
    if key in _VALID_FRESHNESS:
        return key
    return _FRESHNESS.get(key, "")


def _line(result: Dict[str, Any]) -> str:
    title = _clean(result.get("title") or "(untitled)")
    body = _shorten(_clean(result.get("body") or result.get("description") or ""), 200)
    link = result.get("href") or result.get("url") or result.get("link") or ""
    domain = urllib.parse.urlparse(str(link)).netloc or "unknown source"
    # News results carry their own byline and timestamp; both belong in a line
    # the assistant is going to read out loud.
    source = _clean(str(result.get("source") or ""))
    when = _clean(str(result.get("age") or result.get("date") or ""))
    origin = source or domain
    stamp = f" ({origin}, {when})" if when else f" ({origin})"
    return f"{title} — {body}{stamp}" if body else f"{title}{stamp}"


def _clean(text: str) -> str:
    text = _JUNK.sub("", html.unescape(_TAG.sub("", str(text or ""))))
    return " ".join(text.split())


def _shorten(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"
