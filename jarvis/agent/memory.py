"""Persistent memory (design §6.5).

Two of the design's three memory tiers, in one local SQLite file:

  episodic  — what was said, when, and which brain answered. Survives
              reconnects and restarts, so a conversation can be picked up
              again instead of being rebuilt from an in-memory list.
  semantic  — durable facts the assistant was told to keep ("remember that
              the client deadline is Friday"), written deliberately rather
              than by appending every message.

Every JARVIS host (the orb, the Telegram bot, scripts) resolves to the ONE
file below through :func:`shared_memory_path`, so a conversation on any
channel is visible to every channel on its next turn. No sync layer: the
SQLite file is the shared state.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

JARVIS_ROOT = Path(__file__).resolve().parents[2]


def shared_memory_path() -> Path:
    """The single memory.db every JARVIS host reads and writes.

    Resolution is deliberately plain: ``$OPENJARVIS_HOME`` when set, else
    ``JARVIS/.openjarvis``. The orb's old ``governance_home()`` heuristic
    (env home only when it held a skill-governance ledger) silently split
    the file from Brain's the moment the env var pointed elsewhere — hosts
    that share memory must use this one function, and leave
    ``governance_home()`` for the ledger and audit state.
    """
    home = os.environ.get("OPENJARVIS_HOME") or str(JARVIS_ROOT / ".openjarvis")
    return Path(home) / "memory.db"

# Only a statement that *starts* with "remember" is an instruction to keep a
# fact. "do you remember that we shipped?" is a question, not a fact.
_FACT_RE = re.compile(
    r"^\s*(?:please\s+|hey\s+)?remember(?:\s+that)?\s+(?P<body>.{3,200})$",
    re.I,
)


@dataclass(slots=True)
class Turn:
    ts: float
    role: str
    content: str
    engine: str = ""
    route_reason: str = ""


class Memory:
    """Episodic + semantic store for one JARVIS install.

    Holds two kinds of semantic data in the same file:
      facts      — verbatim "remember that …" statements the owner dictated
      user model — durable facts derived from conversation (see
                   jarvis.agent.user_model, which takes the db path and owns
                   those tables)
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS episodes (
                    id INTEGER PRIMARY KEY,
                    ts REAL NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                    content TEXT NOT NULL,
                    engine TEXT NOT NULL DEFAULT '',
                    route_reason TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS episodes_ts ON episodes(ts);
                CREATE TABLE IF NOT EXISTS facts (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- episodic ---------------------------------------------------------
    def remember_turn(self, role: str, content: str, *, engine: str = "", route_reason: str = "") -> None:
        if role not in ("user", "assistant") or not content:
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO episodes(ts, role, content, engine, route_reason) VALUES (?,?,?,?,?)",
                (time.time(), role, content[:4000], engine, route_reason),
            )

    def recent_turns(self, limit: int = 12) -> list[Turn]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, role, content, engine, route_reason FROM episodes "
                "ORDER BY id DESC LIMIT ?",
                (max(0, limit),),
            ).fetchall()
        return [Turn(r["ts"], r["role"], r["content"], r["engine"], r["route_reason"]) for r in reversed(rows)]

    # -- semantic ---------------------------------------------------------
    def maybe_store_fact(self, text: str) -> Optional[str]:
        """Store an explicit 'remember that …' statement; returns the fact."""
        statement = (text or "").strip()
        if "?" in statement:
            return None
        m = _FACT_RE.match(statement)
        if not m:
            return None
        body = m.group("body").strip(" .,!?\n")
        if not body:
            return None
        key = body[:60]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO facts(key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, body, time.time()),
            )
        return body

    def facts(self, limit: int = 20) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT value FROM facts ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [r["value"] for r in rows]

    def stats(self) -> dict:
        with self._connect() as conn:
            episodes = conn.execute("SELECT COUNT(*) AS n FROM episodes").fetchone()["n"]
            facts = conn.execute("SELECT COUNT(*) AS n FROM facts").fetchone()["n"]
        return {"episodes": episodes, "facts": facts, "db": str(self.db_path)}


__all__ = ["Memory", "Turn", "shared_memory_path"]
