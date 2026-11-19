"""Local SQLite project and deadline store."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional


class ProjectStore:
    def __init__(self, db_path: str | Path = "jarvis_projects.db") -> None:
        self.db_path = str(db_path)
        # An in-memory database exists per-connection, so the usual
        # open-per-operation pattern would silently create the schema in one
        # connection and then find no tables in the next. Hold one for :memory:.
        self._shared: Optional[sqlite3.Connection] = None
        if self.db_path == ":memory:":
            self._shared = sqlite3.connect(":memory:", timeout=10)
            self._shared.row_factory = sqlite3.Row
            self._shared.execute("PRAGMA foreign_keys=ON")
        self._init_db()

    @contextmanager
    def _connect(self):
        if self._shared is not None:
            try:
                yield self._shared
                self._shared.commit()
            except Exception:
                self._shared.rollback()
                raise
            return
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deadlines (
                    id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
                    title TEXT NOT NULL, due_date TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','completed','cancelled')),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deadline_alerts (
                    deadline_id INTEGER PRIMARY KEY REFERENCES deadlines(id),
                    alerted_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def add_project(self, name: str, path: str) -> int:
        normalized = str(Path(path).expanduser().resolve())
        with self._connect() as conn:
            existing = conn.execute("SELECT id FROM projects WHERE path=?", (normalized,)).fetchone()
            if existing:
                conn.execute("UPDATE projects SET name=? WHERE id=?", (name, existing[0]))
                return int(existing[0])
            cur = conn.execute("INSERT INTO projects(name,path,created_at) VALUES(?,?,?)", (name, normalized, self._now()))
            return int(cur.lastrowid)

    def get_project(self, project_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            return dict(row) if row else None

    def list_projects(self) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY name")]

    def add_deadline(self, project_id: int, title: str, due_date: str) -> int:
        due = datetime.fromisoformat(due_date)
        if due.tzinfo is None:
            due = due.replace(tzinfo=datetime.now().astimezone().tzinfo)
        with self._connect() as conn:
            cur = conn.execute("INSERT INTO deadlines(project_id,title,due_date,created_at) VALUES(?,?,?,?)", (project_id, title, due.isoformat(timespec="seconds"), self._now()))
            return int(cur.lastrowid)

    def get_deadline(self, deadline_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM deadlines WHERE id=?", (deadline_id,)).fetchone()
            return dict(row) if row else None

    def list_deadlines(self, project_id: Optional[int] = None, status: str = "open") -> list[dict]:
        if status not in ("open", "completed", "cancelled", "all"):
            raise ValueError("status must be open, completed, cancelled, or all")
        sql = "SELECT * FROM deadlines WHERE 1=1"
        params: list = []
        if status != "all":
            sql += " AND status=?"; params.append(status)
        if project_id is not None:
            sql += " AND project_id=?"; params.append(project_id)
        sql += " ORDER BY due_date"
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, params)]

    def update_deadline_status(self, deadline_id: int, new_status: str) -> bool:
        if new_status not in ("open", "completed", "cancelled"):
            raise ValueError("invalid deadline status")
        with self._connect() as conn:
            cur = conn.execute("UPDATE deadlines SET status=? WHERE id=?", (new_status, deadline_id))
            # Status changes reset the reminder: a reopened deadline should
            # alert again, and a closed one should not linger in the log.
            conn.execute("DELETE FROM deadline_alerts WHERE deadline_id=?", (deadline_id,))
            return cur.rowcount > 0

    def get_proactive_deadlines(self, now: Optional[datetime] = None) -> list[dict]:
        """Open deadlines that need attention: overdue *or* inside 48 hours.

        The old window started at `now`, so a deadline that slipped past its
        date vanished from every proactive alert until it was completed.
        """
        now = now or datetime.now().astimezone()
        cutoff = now + timedelta(hours=48)
        with self._connect() as conn:
            rows = conn.execute("""SELECT d.*, p.name AS project_name FROM deadlines d
                JOIN projects p ON p.id=d.project_id WHERE d.status='open'
                AND d.due_date<=? ORDER BY d.due_date""",
                (cutoff.isoformat(timespec="seconds"),))
            return [dict(r) for r in rows]

    def mark_deadline_alerted(self, deadline_id: int) -> None:
        """Record that this deadline was spoken about, so a worker restart
        does not re-announce it."""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO deadline_alerts(deadline_id, alerted_at) VALUES (?,?)",
                (deadline_id, self._now()),
            )

    def alerted_deadline_ids(self) -> set[int]:
        with self._connect() as conn:
            return {int(r[0]) for r in conn.execute("SELECT deadline_id FROM deadline_alerts")}

    def clear_deadline_alert(self, deadline_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM deadline_alerts WHERE deadline_id=?", (deadline_id,))

    def close(self) -> None:
        """Release the shared connection for in-memory stores."""
        if self._shared is not None:
            self._shared.close()
            self._shared = None
