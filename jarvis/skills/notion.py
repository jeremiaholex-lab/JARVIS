"""Read-only Notion notes connector via the Notion API.

Auth is a *static internal-integration token* (``Bearer``), not an OAuth
device flow: the user creates an integration at notion.so/my-integrations,
drops its secret in ``.env`` as ``JARVIS_NOTION_TOKEN``, and shares each
page or database with that integration. A Notion integration can only see
what is explicitly shared with it, so "search returns nothing" usually means
nothing has been connected yet — that empty case is surfaced loudly, not
silently. Read-only by construction: only ``/search`` and ``/blocks`` are
ever called.
"""
from __future__ import annotations

import os
from typing import Dict, List

_BASE = "https://api.notion.com/v1"
_VERSION = "2022-06-28"
_MAX_BLOCK_CHARS = 4000  # bound a note's body so a huge page can't flood the prompt


class NotionConnector:
    def __init__(self, token: str | None = None):
        self.token = (token or os.environ.get("JARVIS_NOTION_TOKEN", "")).strip()

    def _headers(self) -> Dict[str, str]:
        if not self.token:
            raise RuntimeError(
                "Notion is not configured. Set JARVIS_NOTION_TOKEN in .env to your "
                "internal-integration token (notion.so/my-integrations), then share "
                "the pages/databases you want JARVIS to read with that integration."
            )
        return {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": _VERSION,
            "Content-Type": "application/json",
        }

    def _post(self, path: str, body: Dict) -> Dict:
        import requests

        response = requests.post(f"{_BASE}{path}", headers=self._headers(), json=body, timeout=20)
        response.raise_for_status()
        return response.json()

    def _get_blocks(self, page_id: str) -> List[Dict]:
        import requests

        response = requests.get(
            f"{_BASE}/blocks/{page_id}",
            headers=self._headers(),
            params={"page_size": 100},
            timeout=20,
        )
        response.raise_for_status()
        return response.json().get("results", [])

    def _search(self, query: str, limit: int) -> List[Dict]:
        # Empty query to /search lists everything the integration can see —
        # that is how the 'databases' view works.
        body: Dict[str, object] = {"page_size": max(1, min(limit, 20))}
        if query:
            body["query"] = query
        return self._post("/search", body).get("results", [])

    @staticmethod
    def _page_title(obj: Dict) -> str:
        """Readable name for a search result: databases carry a top-level title,
        pages keep theirs under properties.title (a rich_text array)."""
        if obj.get("object") == "database":
            titles = obj.get("title") or []
            return (titles[0].get("plain_text") if titles else "(untitled database)") or "(untitled database)"
        rich = (obj.get("properties", {}).get("title", {}).get("rich_text")) or []
        return (rich[0].get("plain_text") if rich else "") or "(untitled)"

    @staticmethod
    def _block_text(block: Dict) -> str:
        """Flatten one block to plain text: its inline rich_text, else the
        joined text of its child blocks (toggles, callouts, lists)."""
        payload = block.get(block.get("type", ""), {})
        text = "".join(t.get("plain_text", "") for t in payload.get("rich_text") or [])
        if not text:
            children = [NotionConnector._block_text(b) for b in payload.get("sub_blocks") or []]
            text = "\n".join(c for c in children if c)
        return text

    def _page_body(self, page_id: str, limit: int) -> List[str]:
        lines: List[str] = []
        total = 0
        for block in self._get_blocks(page_id):
            text = self._block_text(block).strip()
            if not text:
                continue
            if total + len(text) > _MAX_BLOCK_CHARS:
                lines.append("… (note continues)")
                break
            total += len(text)
            lines.append(text)
        return lines or ["(page has no readable text)"]

    def fetch_view(self, view: str, limit: int = 8, query: str = "") -> List[str]:
        """One read-only Notion view, as pre-formatted strings the model reads.

        view='search'    matching pages/databases as "title [type] url"
        view='note'      the readable body text of the best-matching page
        view='databases' names of databases shared with the integration
        """
        limit = max(1, min(int(limit or 8), 20))
        if view == "databases":
            names = [self._page_title(o) for o in self._search("", limit) if o.get("object") == "database"]
            return names or ["(no databases are shared with this integration yet)"]
        if view in ("search", "note"):
            if not query.strip():
                raise ValueError(f"view {view!r} needs a non-empty query")
            hits = self._search(query, limit if view == "search" else 3)
            if not hits:
                return ["(nothing matched — share pages with this Notion integration so JARVIS can see them)"]
            if view == "search":
                return [
                    f"{self._page_title(o)} [{o.get('object')}] {o.get('url', '')}".strip() for o in hits
                ]
            page = next((o for o in hits if o.get("object") == "page"), None)
            if page is None:
                return [f"(top hit is a database: {self._page_title(hits[0])})"]
            return self._page_body(page["id"], limit)
        raise ValueError("view must be search, note, or databases")


__all__ = ["NotionConnector"]
