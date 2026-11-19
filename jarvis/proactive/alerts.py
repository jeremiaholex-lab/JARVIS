"""Raised issues (design §6.9) — durable, deduplicated, and never self-acting.

The design's proactive rule is explicit: JARVIS may *raise* an issue, but
acting on it still goes through the permission tiers. So this store holds
signals only. It has no way to execute anything, and nothing here imports the
tool layer.

Signals are keyed so the same condition cannot spam the owner: a key seen
inside its cooldown window is folded into the existing alert (count + last
seen) instead of becoming a new interruption.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

_logger = logging.getLogger(__name__)

SEVERITIES = ("info", "notice", "urgent")
DEFAULT_COOLDOWN_S = 6 * 3600.0


@dataclass(slots=True)
class Signal:
    key: str
    source: str
    severity: str
    text: str
    suggested_action: str = ""
    cooldown_s: float = DEFAULT_COOLDOWN_S

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}")
        if not self.key or not self.text:
            raise ValueError("a signal needs a key and a text")


@dataclass(slots=True)
class Alert:
    key: str
    source: str
    severity: str
    text: str
    suggested_action: str
    first_seen: float
    last_seen: float
    count: int
    acknowledged_at: float = 0.0

    @property
    def open(self) -> bool:
        return self.acknowledged_at == 0.0

    def as_dict(self) -> dict:
        data = asdict(self)
        data["open"] = self.open
        return data


class AlertStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS alerts (
                    key TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    text TEXT NOT NULL,
                    suggested_action TEXT NOT NULL DEFAULT '',
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    count INTEGER NOT NULL DEFAULT 1,
                    acknowledged_at REAL NOT NULL DEFAULT 0.0
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

    def raise_signal(self, signal: Signal) -> tuple[Alert, bool]:
        """Record a signal. Returns (alert, is_new_interruption).

        `is_new_interruption` is False while the same key is inside its
        cooldown, which is what lets the watcher speak once and stay quiet.

        The returned Alert is built from the values already in hand rather
        than re-queried: reading through a second connection inside this
        transaction cannot see the row this one has not committed yet.
        """
        now = time.time()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM alerts WHERE key=?", (signal.key,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO alerts(key, source, severity, text, suggested_action,"
                    " first_seen, last_seen, count) VALUES (?,?,?,?,?,?,?,1)",
                    (signal.key, signal.source, signal.severity, signal.text,
                     signal.suggested_action, now, now),
                )
                alert = Alert(
                    key=signal.key, source=signal.source, severity=signal.severity,
                    text=signal.text, suggested_action=signal.suggested_action,
                    first_seen=now, last_seen=now, count=1, acknowledged_at=0.0,
                )
                return alert, True
            fresh = ((now - row["last_seen"]) >= max(0.0, signal.cooldown_s)
                     or bool(row["acknowledged_at"]))
            conn.execute(
                "UPDATE alerts SET last_seen=?, count=count+1, text=?, severity=?, suggested_action=? "
                "WHERE key=?",
                (now, signal.text, signal.severity, signal.suggested_action, signal.key),
            )
            alert = Alert(
                key=signal.key, source=signal.source, severity=signal.severity,
                text=signal.text, suggested_action=signal.suggested_action,
                first_seen=row["first_seen"], last_seen=now, count=row["count"] + 1,
                acknowledged_at=row["acknowledged_at"],
            )
            return alert, bool(fresh)

    def get(self, key: str) -> Alert:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM alerts WHERE key=?", (key,)).fetchone()
        return _alert(row)

    def open_alerts(self, limit: int = 20) -> list[Alert]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE acknowledged_at=0.0 "
                "ORDER BY CASE severity WHEN 'urgent' THEN 0 WHEN 'notice' THEN 1 ELSE 2 END,"
                " last_seen DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_alert(r) for r in rows]

    def acknowledge(self, key: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE alerts SET acknowledged_at=? WHERE key=? AND acknowledged_at=0.0",
                (time.time(), key),
            )
            return cur.rowcount > 0

    def stats(self) -> dict:
        with self._connect() as conn:
            open_count = conn.execute(
                "SELECT COUNT(*) AS n FROM alerts WHERE acknowledged_at=0.0").fetchone()["n"]
            total = conn.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()["n"]
        return {"open": open_count, "total": total, "db": str(self.db_path)}


def _alert(row: sqlite3.Row) -> Alert:
    return Alert(
        key=row["key"], source=row["source"], severity=row["severity"], text=row["text"],
        suggested_action=row["suggested_action"], first_seen=row["first_seen"],
        last_seen=row["last_seen"], count=row["count"], acknowledged_at=row["acknowledged_at"],
    )


__all__ = ["Signal", "Alert", "AlertStore", "SEVERITIES"]
