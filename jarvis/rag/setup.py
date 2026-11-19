"""RAG setup + search CLI (§6.8).

Indexes the user's chosen folders into the SQLite vector store, and answers
queries with ranked, cited chunks so agents can reason about the user's own
files during a turn.

Usage (runtime venv, from the JARVIS root):
  python -m jarvis.rag.setup                     # index the default folders
  python -m jarvis.rag.setup C:/notes C:/projects # index specific folders
  python -m jarvis.rag.setup --ask "query"        # search + cite
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from openjarvis.core.paths import get_config_dir

from .store import RagStore

# Module logger
_logger = logging.getLogger(__name__)

_DB = Path(get_config_dir()) / "rag.db"
# Design §8: sensible default = the user's active project folder, confirmed
# at first run. Re-pointable here by passing folders explicitly.
_DEFAULT_FOLDERS = [Path(__file__).resolve().parents[2], Path(get_config_dir()) / "traces"]


def default_folders() -> list[Path]:
    return [Path(p).resolve() for p in _DEFAULT_FOLDERS]


def setup(folders=None, *, force: bool = False) -> dict:
    """Index the chosen folders and report what happened."""
    folders = [Path(f).resolve() for f in folders] if folders else default_folders()
    _logger.info("indexing RAG store: %s", [str(f) for f in folders])
    store = RagStore(_DB, folders=folders)
    written = store.index(force=force)
    _logger.info("RAG indexing complete: %d chunk(s) written", written)
    return {"indexed_chunks": written, "stats": store.stats(), "folders": [str(f) for f in folders], "db": str(_DB)}


def ask(query: str, top_k: int = 5) -> dict:
    store = RagStore(_DB, folders=default_folders())
    hits = store.search(query, top_k=top_k)
    return {
        "query": query,
        "results": [
            {"rank": i + 1, "score": h.score, "source": h.source, "heading": h.heading, "snippet": h.snippet}
            for i, h in enumerate(hits)
        ],
        "citations": [f"[{i+1}] {h.source}" for i, h in enumerate(hits)],
        "stats": store.stats(),
    }


def main(argv=None) -> int:
    from jarvis.logging import setup_from_env

    setup_from_env()
    ap = argparse.ArgumentParser(prog="jarvis.rag.setup")
    ap.add_argument("folders", nargs="*", help="Folders to index (default: JARVIS project + traces)")
    ap.add_argument("--force", action="store_true", help="Re-embed everything")
    ap.add_argument("--ask", metavar="QUERY", help="Search the index instead of re-indexing")
    ap.add_argument("--top-k", type=int, default=5)
    args = ap.parse_args(argv)

    if args.ask:
        print(json.dumps(ask(args.ask, top_k=args.top_k), indent=2))
        return 0
    print(json.dumps(setup(args.folders or None, force=args.force), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
