"""Web search: a renamed package and a flaky endpoint must not read as broken.

Two independent real-world failures this connector has to survive:

* the package installs as ``ddgs`` now and used to install as
  ``duckduckgo_search`` - the import name alone must never look like
  "web search is not installed" (the bug JARVIS reported to its owner);
* the free endpoint answers "No results found" for a live query about a third
  of the time and the same query works on another backend seconds later, so
  backends are rotated and a genuinely empty answer is not an error.
"""
from __future__ import annotations

import json
import sys
import types

import pytest

from jarvis.agent.tools import build_tools
from jarvis.skills.web_search import WebSearchConnector, _freshness

_ROWS = [
    {"title": "Model <b>ships</b> update", "href": "https://example.com/a",
     "body": "It does a thing \ufffd today"},
    {"title": "Second story", "url": "https://other.org/b", "description": ""},
    {"title": "No link at all"},
]

_NEWS = [
    {"title": "Chipmaker buys rival", "url": "https://news.example/x",
     "body": "The deal closes in spring", "source": "Wire Service", "date": "3h"},
]


class _FakeDDGS:
    """Stands in for DDGS; `fail` is (method, backend) pairs that say "no
    results", and `error` raises a transport fault instead."""

    calls: list = []
    fail: tuple = ()
    fail_all: bool = False
    error: Exception | None = None
    last_kwargs: dict | None = None
    no_news: bool = False

    def __init__(self):
        pass

    def text(self, query, **kwargs):
        return self._answer("text", query, **kwargs)

    def news(self, query, **kwargs):
        if type(self).no_news:
            raise AttributeError("news")
        return self._answer("news", query, **kwargs)

    def _answer(self, method, query, **kwargs):
        backend = kwargs.get("backend", "auto")
        type(self).calls.append((method, backend))
        type(self).last_kwargs = kwargs
        if type(self).error is not None:
            raise type(self).error
        if type(self).fail_all or (method, backend) in type(self).fail:
            raise RuntimeError("No results found.")
        return list(_NEWS) if method == "news" else list(_ROWS)


@pytest.fixture
def ddgs_module(monkeypatch):
    module = types.ModuleType("ddgs")
    module.DDGS = _FakeDDGS
    monkeypatch.setitem(sys.modules, "ddgs", module)
    monkeypatch.setitem(sys.modules, "duckduckgo_search", None)   # legacy absent
    _FakeDDGS.calls = []
    _FakeDDGS.fail = ()
    _FakeDDGS.fail_all = False
    _FakeDDGS.error = None
    _FakeDDGS.no_news = False
    _FakeDDGS.last_kwargs = None
    return _FakeDDGS


def test_search_returns_cited_lines_and_strips_markup(ddgs_module):
    lines = WebSearchConnector().fetch_view("latest python api changes")
    assert lines[0] == "Model ships update — It does a thing today (example.com)"
    assert lines[1] == "Second story (other.org)"          # url key, empty body
    assert lines[2] == "No link at all (unknown source)"
    assert ddgs_module.calls == [("text", "auto")]         # reference query, one call
    assert ddgs_module.last_kwargs == {"max_results": 5, "safesearch": "moderate"}


def test_time_bounded_query_searches_news_and_cites_source_and_age(ddgs_module):
    lines = WebSearchConnector().fetch_view("technology headlines", freshness="day")
    assert lines == ["Chipmaker buys rival — The deal closes in spring (Wire Service, 3h)"]
    assert ddgs_module.calls == [("news", "auto")]
    assert ddgs_module.last_kwargs["timelimit"] == "d"


def test_explicit_kind_beats_the_default(ddgs_module):
    WebSearchConnector().fetch_view("python tutorial", kind="news")
    assert ddgs_module.calls[0] == ("news", "auto")
    _FakeDDGS.calls = []
    WebSearchConnector().fetch_view("tech news", kind="web", freshness="w")
    assert _FakeDDGS.calls[0] == ("text", "auto")


def test_news_only_build_falls_back_to_text(ddgs_module):
    _FakeDDGS.no_news = True
    lines = WebSearchConnector().fetch_view("headlines", freshness="d")
    assert lines and lines[0].endswith("(example.com)")


def test_flaky_endpoint_rotates_to_the_next_backend(ddgs_module):
    ddgs_module.fail = (("text", "auto"), ("text", "lite"), ("news", "auto"))
    lines = WebSearchConnector().fetch_view("latest model updates")
    assert lines and ddgs_module.calls[:2] == [("text", "auto"), ("text", "lite")]


def test_genuinely_empty_answers_are_not_an_error(ddgs_module):
    ddgs_module.fail_all = True
    assert WebSearchConnector().fetch_view("a phrase nobody ever wrote about") == []


def test_transport_failure_says_endpoint_not_dependency(ddgs_module):
    ddgs_module.error = ConnectionError("getaddrinfo failed")
    with pytest.raises(RuntimeError) as exc:
        WebSearchConnector().fetch_view("anything")
    assert "endpoint unavailable (ConnectionError)" in str(exc.value)
    assert "install" not in str(exc.value)


def test_legacy_package_name_still_works(monkeypatch):
    legacy = types.ModuleType("duckduckgo_search")
    legacy.DDGS = _FakeDDGS
    _FakeDDGS.calls = []
    _FakeDDGS.fail = ()
    _FakeDDGS.fail_all = False
    _FakeDDGS.error = None
    _FakeDDGS.no_news = False
    monkeypatch.setitem(sys.modules, "ddgs", None)          # current name absent
    monkeypatch.setitem(sys.modules, "duckduckgo_search", legacy)
    assert len(WebSearchConnector().fetch_view("anything")) == 3


def test_missing_both_names_says_so(monkeypatch):
    monkeypatch.setitem(sys.modules, "ddgs", None)
    monkeypatch.setitem(sys.modules, "duckduckgo_search", None)
    with pytest.raises(RuntimeError) as exc:
        WebSearchConnector().fetch_view("anything")
    assert "not installed" in str(exc.value)


def test_versions_without_a_backend_argument_still_work(monkeypatch):
    class Old:
        def text(self, query, **kwargs):
            if "backend" in kwargs:
                raise TypeError("text() got an unexpected keyword argument 'backend'")
            return list(_ROWS)

        news = text

    module = types.ModuleType("ddgs")
    module.DDGS = Old
    monkeypatch.setitem(sys.modules, "ddgs", module)
    assert len(WebSearchConnector().fetch_view("anything")) == 3


@pytest.mark.parametrize("given,want", [
    ("d", "d"), ("day", "d"), ("today", "d"), ("24h", "d"),
    ("W", "w"), ("week", "w"), ("m", "m"), ("month", "m"), ("y", "y"),
    ("", ""), ("hourly", ""), ("nonsense", ""), (None, ""),
])
def test_freshness_normalisation(given, want):
    assert _freshness(given) == want


def test_blank_query_never_hits_the_network(ddgs_module):
    assert WebSearchConnector().fetch_view("   ") == []
    assert ddgs_module.calls == []


def test_tool_wrapper_reports_a_clean_error(ddgs_module):
    ddgs_module.error = ConnectionError("getaddrinfo failed")
    web = next(t for t in build_tools() if t.name == "web_search")
    payload = json.loads(web.fn("news"))
    assert "endpoint unavailable" in payload["error"]
    assert "install" not in payload["error"]


def test_live_search_end_to_end():
    """Real endpoint, whatever package name this interpreter has."""
    try:
        connector = WebSearchConnector()
    except RuntimeError as exc:                       # pragma: no cover
        pytest.skip(str(exc))
    try:
        results = connector.fetch_view("python 3.14 release notes", limit=3)
        news = connector.fetch_view("technology", limit=3, freshness="d")
    except RuntimeError as exc:
        pytest.skip(f"endpoint unreachable from here: {exc}")
    assert isinstance(results, list) and isinstance(news, list)
    for line in results + news:
        assert "<" not in line and "\ufffd" not in line
