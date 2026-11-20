"""Recurring cron schedules that enqueue read-only tasks (design:
2026-11-20-cron-schedule-layer).

Schedules live in the same SQLite file as the durable task queue. A pump
(``ScheduleStore.check``) runs from Brain's task worker loop: when a slot is
due it enqueues ONE read-only task through the existing ``TaskStore`` and
resynchronizes the schedule to the next future slot. Execution is entirely
the runner's job; this module only decides *when* work is queued.

Catch-up semantics: missed slots never backfill. Whatever is due at a
check, the schedule fires late ONCE and re-anchors to the next slot
strictly after *now*. The enqueue carries the idempotency key
``schedule:{id}:{first_missed_slot}`` so a crash between the enqueue and
the bookkeeping cannot queue the same slot twice.
"""
from __future__ import annotations

import datetime as _dt
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from jarvis.agent.task_runner import TaskStore

MAX_SCHEDULES = 50
MAX_INSTRUCTION_CHARS = 20_000
MAX_CRON_CHARS = 120
_MAX_SCAN_MINUTES = 366 * 24 * 60  # scan cap: impossible expressions yield 0.0

_FIELD_NAMES = ("minute", "hour", "day_of_month", "month", "day_of_week")
# day_of_week keeps the 0-7 range in the parser; 7 is normalized to 0
# (Sunday) after parsing, so matching only ever deals with 0-6.
_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


class CronError(ValueError):
    """The expression is outside the restricted grammar, or no slot exists
    within the scan cap."""


def _parse_field(field: str, name: str, lo: int, hi: int) -> frozenset[int]:
    values: set[int] = set()
    for part in field.split(","):
        if not part or len(part) > 12:
            raise CronError(f"bad {name} field {part!r}: empty or too long")
        m = re.fullmatch(r"(?:(\*|\d+)(?:-(\d+))?(?:/(\d+))?)", part)
        if not m:
            raise CronError(
                f"bad {name} field {part!r}: expected *, N, A-B, or A-B/step, "
                "combined with commas")
        star = part.startswith("*")
        first_s, last_s, step_s = m.group(1), m.group(2), m.group(3)
        if star:
            start, end = lo, hi
        elif last_s:
            start, end = int(first_s), int(last_s)
            if start > end:
                raise CronError(f"bad {name} range {part!r}: start exceeds end "
                                "(no wraparound)")
        else:
            if step_s:
                raise CronError(f"bad {name} field {part!r}: step needs * or a range")
            start = end = int(first_s)
        if start < lo or end > hi:
            raise CronError(f"bad {name} value in {part!r}: out of range {lo}-{hi}")
        step = int(step_s) if step_s else 1
        if not 1 <= step <= 60:
            raise CronError(f"bad {name} step in {part!r}: 1-60 allowed")
        values.update(range(start, end + 1, step))
    return frozenset(values)


def parse_cron(expr: str) -> dict:
    """Parse the restricted 5-field cron grammar.

    Fields: minute hour day-of-month month day-of-week. Local machine time.
    ``*``, single numbers, comma lists, and ``A-B/step`` ranges (``*/n``
    sugar). Day-of-week accepts 0-7, both 0 and 7 meaning Sunday. No
    month/day names, no L/W/#. Raises :class:`CronError` otherwise.
    """
    if not isinstance(expr, str):
        raise CronError("cron expression must be text")
    expr = expr.strip()
    if len(expr) > MAX_CRON_CHARS:
        raise CronError(f"cron expression too long (max {MAX_CRON_CHARS} chars)")
    fields = expr.split()
    if len(fields) != 5:
        raise CronError(
            f"expected 5 fields (minute hour dom month dow), got {len(fields)}")
    parsed: dict = {}
    for i, (name, (lo, hi)) in enumerate(zip(_FIELD_NAMES, _FIELD_RANGES)):
        parsed[name] = _parse_field(fields[i], name, lo, hi)
    parsed["day_of_week"] = frozenset(0 if d == 7 else d for d in parsed["day_of_week"])
    parsed["dom_restricted"] = fields[2] != "*"
    parsed["dow_restricted"] = fields[4] != "*"
    return parsed


def next_slot(spec_or_expr, after_ts: float) -> float:
    """First matching slot strictly after ``after_ts`` (epoch seconds).

    Walks forward in whole minutes of local time; returns ``0.0`` when no
    slot exists within the 366-day scan cap (e.g. Feb 30).
    """
    spec = parse_cron(spec_or_expr) if isinstance(spec_or_expr, str) else spec_or_expr
    start = _dt.datetime.fromtimestamp(after_ts).replace(second=0, microsecond=0)
    for i in range(1, _MAX_SCAN_MINUTES + 1):
        candidate = start + _dt.timedelta(minutes=i)
        if candidate.minute not in spec["minute"]:
            continue
        if candidate.hour not in spec["hour"]:
            continue
        if candidate.month not in spec["month"]:
            continue
        dom_ok = candidate.day in spec["day_of_month"]
        dow_ok = (candidate.weekday() + 1) % 7 in spec["day_of_week"]
        if spec["dom_restricted"] and spec["dow_restricted"]:
            # Classic cron: both restricted -> OR semantics.
            if dom_ok or dow_ok:
                return candidate.timestamp()
        elif dom_ok and dow_ok:
            return candidate.timestamp()
    return 0.0


@dataclass(slots=True)
class Schedule:
    id: str
    cron: str
    instruction: str
    enabled: bool
    last_fired_at: float
    next_run_at: float
    last_task_id: str
    created_at: float


class ScheduleStore:
    """SQLite-backed recurring schedules; the pump enqueues due slots."""

    def __init__(self, db_path: str | Path, tasks: TaskStore):
        self.db_path = Path(db_path)
        self.tasks = tasks
        self._lock = threading.RLock()
        with self._connection() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS agent_schedules (
                    id TEXT PRIMARY KEY,
                    cron TEXT NOT NULL,
                    instruction TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_fired_at REAL NOT NULL DEFAULT 0,
                    next_run_at REAL NOT NULL,
                    last_task_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                )
            """)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(str(self.db_path), timeout=10, isolation_level="IMMEDIATE")
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _decode(row) -> Optional[Schedule]:
        if row is None:
            return None
        return Schedule(
            id=row["id"], cron=row["cron"], instruction=row["instruction"],
            enabled=bool(row["enabled"]), last_fired_at=row["last_fired_at"],
            next_run_at=row["next_run_at"], last_task_id=row["last_task_id"],
            created_at=row["created_at"],
        )

    def create(self, cron: str, instruction: str) -> Schedule:
        spec = parse_cron(cron)  # validates grammar
        instruction = str(instruction or "").strip()
        if not instruction:
            raise ValueError("instruction must not be empty")
        if len(instruction) > MAX_INSTRUCTION_CHARS:
            raise ValueError(f"instruction must be at most {MAX_INSTRUCTION_CHARS} chars")
        now = time.time()
        first = next_slot(spec, now)
        if first == 0.0:
            raise CronError(f"no slot matches {cron!r} within a year; "
                            "check the month/day/weekday values")
        with self._lock, self._connection() as db:
            count = db.execute("SELECT COUNT(*) FROM agent_schedules").fetchone()[0]
            if count >= MAX_SCHEDULES:
                raise ValueError(f"schedule limit reached ({MAX_SCHEDULES} max)")
            schedule_id = uuid.uuid4().hex
            db.execute(
                """INSERT INTO agent_schedules
                   (id, cron, instruction, enabled, last_fired_at, next_run_at,
                    last_task_id, created_at) VALUES (?,?,?,1,0,?,'',?)""",
                (schedule_id, cron.strip(), instruction, first, now),
            )
            row = db.execute("SELECT * FROM agent_schedules WHERE id=?",
                             (schedule_id,)).fetchone()
        return self._decode(row)

    def get(self, schedule_id: str) -> Optional[Schedule]:
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM agent_schedules WHERE id=?",
                             (schedule_id,)).fetchone()
        return self._decode(row)

    def list(self) -> list[Schedule]:
        with self._lock, self._connection() as db:
            rows = db.execute("SELECT * FROM agent_schedules ORDER BY created_at").fetchall()
        return [self._decode(r) for r in rows]

    def set_enabled(self, schedule_id: str, enabled: bool) -> Schedule:
        with self._lock, self._connection() as db:
            self._require(db, schedule_id)
            if enabled:
                row = db.execute("SELECT cron FROM agent_schedules WHERE id=?",
                                 (schedule_id,)).fetchone()
                future = next_slot(parse_cron(row["cron"]), time.time())
                if future == 0.0:
                    raise CronError("resume failed: no slot within a year")
                db.execute("UPDATE agent_schedules SET enabled=1, next_run_at=? WHERE id=?",
                           (future, schedule_id))
            else:
                db.execute("UPDATE agent_schedules SET enabled=0 WHERE id=?", (schedule_id,))
            row = db.execute("SELECT * FROM agent_schedules WHERE id=?", (schedule_id,)).fetchone()
        return self._decode(row)

    def remove(self, schedule_id: str) -> None:
        with self._lock, self._connection() as db:
            cur = db.execute("DELETE FROM agent_schedules WHERE id=?", (schedule_id,))
            if cur.rowcount != 1:
                raise KeyError(f"unknown schedule {schedule_id!r}")

    @staticmethod
    def _require(db, schedule_id: str) -> None:
        if db.execute("SELECT id FROM agent_schedules WHERE id=?", (schedule_id,)).fetchone() is None:
            raise KeyError(f"unknown schedule {schedule_id!r}")

    def check(self, now: Optional[float] = None) -> list[dict]:
        """Fire every due enabled schedule once, late if needed.

        Returns one record per fired slot:
        ``{"schedule_id", "due_slot", "task"}``. The enqueue is idempotent:
        a slot already queued by a crashed pump returns the *original*
        ``AgentTask`` (``TaskStore.create`` dedupes on the key), so a restart
        never queues the same slot twice. ``task`` is only None if the
        enqueue raised an unexpected error, which the caller's try/except
        (the worker loop) already handles.
        """
        now = time.time() if now is None else float(now)
        fired: list[dict] = []
        with self._lock, self._connection() as db:
            rows = db.execute("SELECT * FROM agent_schedules WHERE enabled=1").fetchall()
        for row in rows:
            sched = self._decode(row)
            if sched is None:
                continue
            spec = parse_cron(sched.cron)
            if sched.next_run_at == 0.0:
                # Sentinel: created but never pumped (should not happen, since
                # create() stores a real slot). Fire late once, resync to now.
                due = now
                future = next_slot(spec, now)
                if future == 0.0:
                    continue  # impossible expression; leave the row, do not loop
            elif sched.next_run_at < now:
                due = sched.next_run_at
                # Missed slots never backfill: re-anchor to the next slot
                # strictly after now; this one late fire stands for all of them.
                future = next_slot(spec, now)
                if future == 0.0:
                    continue
            else:
                continue
            goal = f"[schedule {sched.id[:8]}] {sched.instruction}"
            idempotency_key = f"schedule:{sched.id}:{int(due)}"
            task = None
            try:
                task = self.tasks.create(goal, idempotency_key=idempotency_key)
            except sqlite3.IntegrityError:
                task = None  # an earlier run already enqueued this slot
            with self._lock, self._connection() as db:
                db.execute(
                    """UPDATE agent_schedules
                       SET last_fired_at=?, next_run_at=?, last_task_id=? WHERE id=?""",
                    (now, future, task.id if task else sched.last_task_id, sched.id),
                )
            fired.append({"schedule_id": sched.id, "due_slot": due, "task": task})
        return fired
