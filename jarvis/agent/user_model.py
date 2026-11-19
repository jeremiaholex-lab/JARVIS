"""The rolling user model (design §6.5).

Episodic memory (jarvis.agent.memory) answers "what was said". The design asks
for more: a *user model* — who this person is, what they work on, how they
like answers — updated as new facts arrive, and a semantic layer that gets
consolidated rather than merely appended.

This module does that in three moves:

  1. extract   — pull candidate facts out of a conversation turn (local model
                 first; cloud only if configured and the local model is silent)
  2. merge     — reconcile against what is already known: same fact → refresh,
                 contradicted fact → replace and keep the old one as history,
                 new fact → add. Never a blind append.
  3. summarise — a short block injected into the system prompt so the
                 assistant behaves like it knows you, not like it read a log.

Everything is stored locally in the same SQLite file as episodic memory, under
a `user_model` table. Nothing is sent anywhere that the turn was not already
going to.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

_logger = logging.getLogger(__name__)

MAX_FACTS_IN_PROMPT = 14
FACT_CHARS = 220

# Cheap pre-filter: no point asking a model about a turn with nothing in it.
_CANDIDATE_RE = re.compile(
    r"\b(i am|i'm|im|my|call me|prefer|i like|i hate|always|never|"
    r"remember|working on|deadline|project|timezone|i use|i work)\b",
    re.I,
)

_EXTRACT_SYSTEM = (
    "You maintain a user model for a personal assistant. From the message, "
    "extract only durable facts about the USER: identity, how to address them, "
    "projects they are working on, preferences about answers, recurring "
    "commitments, tools they use. Ignore one-off questions, small talk, and "
    "anything about the assistant's own behaviour. Respond as JSON: "
    '{"facts": [{"key": "short-stable-slug", "value": "one sentence"}]}. '
    "Empty list if nothing durable was said."
)


@dataclass(slots=True)
class Fact:
    key: str
    value: str
    confidence: float
    updated_at: float
    supersedes: str = ""


class UserModel:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS user_model (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    updated_at REAL NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    retired INTEGER NOT NULL DEFAULT 0
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS user_model_history (
                    id INTEGER PRIMARY KEY,
                    key TEXT NOT NULL,
                    old_value TEXT NOT NULL,
                    new_value TEXT NOT NULL,
                    changed_at REAL NOT NULL,
                    reason TEXT NOT NULL DEFAULT ''
                )"""
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

    # -- read ------------------------------------------------------------
    def active_facts(self, limit: int = MAX_FACTS_IN_PROMPT) -> list[Fact]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value, confidence, updated_at FROM user_model "
                "WHERE retired=0 ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Fact(r["key"], r["value"], r["confidence"], r["updated_at"]) for r in rows]

    def fact(self, key: str) -> Optional[Fact]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT key, value, confidence, updated_at, retired FROM user_model WHERE key=?",
                (key,),
            ).fetchone()
        if not row or row["retired"]:
            return None
        return Fact(row["key"], row["value"], row["confidence"], row["updated_at"])

    def history(self, key: str = "", limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            if key:
                rows = conn.execute(
                    "SELECT key, old_value, new_value, changed_at, reason FROM user_model_history "
                    "WHERE key=? ORDER BY changed_at DESC LIMIT ?", (key, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT key, old_value, new_value, changed_at, reason FROM user_model_history "
                    "ORDER BY changed_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    # -- write -----------------------------------------------------------
    def put(self, key: str, value: str, *, confidence: float = 1.0, source: str = "") -> Fact:
        """Store or replace one fact, keeping the previous value as history."""
        if not (key or "").strip():
            raise ValueError("fact needs a key")
        cleaned_value = (value or "").strip()
        if not cleaned_value:
            raise ValueError("fact needs a value")
        key = _slug(key)
        value = cleaned_value[:FACT_CHARS]
        previous = self.fact(key)
        now = time.time()
        with self._connect() as conn:
            if previous and previous.value != value:
                conn.execute(
                    "INSERT INTO user_model_history(key, old_value, new_value, changed_at, reason) "
                    "VALUES (?,?,?,?,?)",
                    (key, previous.value, value, now, "updated"),
                )
            # retired=excluded.retired matters: re-stating a superseded fact is
            # how it comes back, and without it the row stays hidden forever.
            conn.execute(
                "INSERT INTO user_model(key, value, confidence, updated_at, source, retired) "
                "VALUES (?,?,?,?,?,0) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, confidence=excluded.confidence, "
                "updated_at=excluded.updated_at, source=excluded.source, retired=excluded.retired",
                (key, value, confidence, now, source),
            )
        return Fact(key, value, confidence, now)

    def _raw(self, key: str) -> Optional[Fact]:
        """The row for this key whether active or retired."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT key, value, confidence, updated_at FROM user_model WHERE key=?", (key,)
            ).fetchone()
        if not row:
            return None
        return Fact(row["key"], row["value"], row["confidence"], row["updated_at"])

    def replace(self, key: str, value: str, *, reason: str = "superseded", source: str = "extracted") -> Fact:
        """Change a known fact, recording old -> new as one history entry."""
        cleaned_value = (value or "").strip()[:FACT_CHARS]
        if not cleaned_value:
            raise ValueError("fact needs a value")
        previous = self.fact(key)
        now = time.time()
        with self._connect() as conn:
            # Look at the retired row too: re-stating a fact the owner had
            # retired is a change worth recording, not an invisible resurrection.
            logged = previous or self._raw(key)
            if logged and logged.value != cleaned_value:
                conn.execute(
                    "INSERT INTO user_model_history(key, old_value, new_value, changed_at, reason) "
                    "VALUES (?,?,?,?,?)",
                    (key, logged.value, cleaned_value, now, reason if previous else "reinstated"),
                )
            conn.execute(
                "INSERT INTO user_model(key, value, confidence, updated_at, source, retired) "
                "VALUES (?,?,?,?,?,0) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, confidence=excluded.confidence, "
                "updated_at=excluded.updated_at, source=excluded.source, retired=0",
                (key, cleaned_value, 1.0, now, source),
            )
        return Fact(key, cleaned_value, 1.0, now)

    def retire(self, key: str, *, reason: str = "contradicted") -> bool:
        previous = self.fact(key)
        if previous is None:
            return False
        with self._connect() as conn:
            conn.execute("UPDATE user_model SET retired=1 WHERE key=?", (key,))
            conn.execute(
                "INSERT INTO user_model_history(key, old_value, new_value, changed_at, reason) "
                "VALUES (?,?,?,?,?)",
                (key, previous.value, "", time.time(), reason),
            )
        return True

    def merge(self, facts: Sequence[tuple[str, str]], *, source: str = "extracted") -> list[Fact]:
        """Reconcile candidate facts with what is already known.

        Same idea expressed differently should not create a second row, and a
        changed fact should replace rather than contradict the model later.
        """
        out: list[Fact] = []
        for key, value in facts:
            existing = self.fact(_slug(key))
            if existing and _same_enough(existing.value, value):
                # Refresh recency only; the wording may be slightly different.
                with self._connect() as conn:
                    conn.execute("UPDATE user_model SET updated_at=? WHERE key=?",
                                 (time.time(), existing.key))
                out.append(Fact(existing.key, existing.value, existing.confidence, time.time()))
                continue
            if existing:
                # Changed: replace in one recorded step so the audit trail reads
                # old -> new rather than "removed" then an unlogged re-add.
                replaced = self.replace(existing.key, value, source=source)
                replaced.supersedes = existing.key
                out.append(replaced)
                continue
            out.append(self.put(key, value, source=source))
        return out

    # -- prompt block ----------------------------------------------------
    def prompt_block(self) -> str:
        facts = self.active_facts()
        if not facts:
            return ""
        lines = "\n".join(f"- {f.value}" for f in facts)
        return (
            "What you know about this user (from their own statements; prefer "
            "these over guessing, and never contradict them without saying so):\n"
            + lines
        )

    def stats(self) -> dict:
        with self._connect() as conn:
            active = conn.execute("SELECT COUNT(*) AS n FROM user_model WHERE retired=0").fetchone()["n"]
            retired = conn.execute("SELECT COUNT(*) AS n FROM user_model WHERE retired=1").fetchone()["n"]
            changes = conn.execute("SELECT COUNT(*) AS n FROM user_model_history").fetchone()["n"]
        return {"active": active, "retired": retired, "changes": changes}


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------

def extract_facts(text: str, *, call_engine: Callable[[list[dict]], str]) -> list[tuple[str, str]]:
    """Ask a model for durable facts in this message; [] on any failure."""
    if not _CANDIDATE_RE.search(text or ""):
        return []
    messages = [
        {"role": "system", "content": _EXTRACT_SYSTEM},
        {"role": "user", "content": text[:1500]},
    ]
    try:
        raw = call_engine(messages)
    except Exception as exc:
        _logger.debug("fact extraction skipped: %s", exc)
        return []
    payload = _parse_json_object(raw)
    if not payload:
        return []
    out: list[tuple[str, str]] = []
    for item in payload.get("facts", [])[:6]:
        if isinstance(item, dict) and item.get("key") and item.get("value"):
            out.append((str(item["key"]), str(item["value"])))
    return out


def _parse_json_object(text: str) -> dict:
    if not text:
        return {}
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _slug(key: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", (key or "").casefold()).strip("-")
    return cleaned[:48]


_STOPWORDS = {"a", "an", "the", "is", "are", "to", "of", "and", "in", "on", "for", "me", "i", "my"}


def _same_enough(left: str, right: str) -> bool:
    """Loose equality: same content words, ignoring wording drift."""
    def tokens(value: str) -> set[str]:
        return {w for w in re.findall(r"[a-z0-9]+", value.casefold()) if w not in _STOPWORDS}

    a, b = tokens(left), tokens(right)
    if not a or not b:
        return left.strip().casefold() == right.strip().casefold()
    overlap = len(a & b) / max(1, len(a | b))
    return overlap >= 0.75


__all__ = ["UserModel", "Fact", "extract_facts", "MAX_FACTS_IN_PROMPT"]
