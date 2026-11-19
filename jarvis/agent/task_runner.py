"""Durable, bounded task queue for JARVIS agent workflows.

The store owns task state and checkpoints; the injected processor performs one
bounded unit of work. Checkpoints are committed before the next step, so a
restart can safely requeue interrupted work. Processors should make external
side effects idempotent: a process can die after a side effect but before its
checkpoint is committed.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

STATUSES = {"queued", "running", "waiting_confirmation", "completed", "failed", "cancelled"}
TERMINAL = {"completed", "failed", "cancelled"}
_ALLOWED_TRANSITIONS = {
    "queued": {"running", "cancelled"},
    "running": {"queued", "waiting_confirmation", "completed", "failed", "cancelled"},
    "waiting_confirmation": {"queued", "cancelled"},
    "failed": {"queued", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}


@dataclass(slots=True)
class AgentTask:
    id: str
    goal: str
    status: str
    created_at: float
    updated_at: float
    attempts: int = 0
    steps: int = 0
    checkpoint: dict | None = None
    result: str = ""
    error: str = ""
    idempotency_key: str = ""
    plan_id: str = ""
    step_index: int = -1
    depends_on: list[str] = field(default_factory=list)
    is_plan_root: bool = False


@dataclass(slots=True)
class StepResult:
    """Result of one processor step."""

    done: bool = False
    waiting_confirmation: bool = False
    checkpoint: dict | None = None
    result: str = ""
    error: str = ""


_DEFAULT_STORE: "TaskStore | None" = None


def configure_task_store(store: "TaskStore | None") -> None:
    global _DEFAULT_STORE
    _DEFAULT_STORE = store


def configured_task_store() -> "TaskStore | None":
    return _DEFAULT_STORE


class TaskStore:
    """SQLite-backed task records and append-only lifecycle events."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    steps INTEGER NOT NULL DEFAULT 0,
                    checkpoint_json TEXT,
                    result TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    plan_id TEXT NOT NULL DEFAULT '',
                    step_index INTEGER NOT NULL DEFAULT -1,
                    depends_on TEXT NOT NULL DEFAULT '[]',
                    is_plan_root INTEGER NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS agent_tasks_idempotency
                    ON agent_tasks(idempotency_key) WHERE idempotency_key <> '';
                CREATE INDEX IF NOT EXISTS agent_tasks_queue
                    ON agent_tasks(status, created_at);
                CREATE INDEX IF NOT EXISTS agent_tasks_plan
                    ON agent_tasks(plan_id, step_index);
                CREATE TABLE IF NOT EXISTS agent_task_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    at REAL NOT NULL,
                    event TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}'
                );
            """)
            # Migration for existing databases: add columns if they don't exist
            self._migrate_schema(db)

    def _migrate_schema(self, db) -> None:
        """Add plan columns to existing databases."""
        cols = {r[1] for r in db.execute("PRAGMA table_info(agent_tasks)").fetchall()}
        if "plan_id" not in cols:
            db.execute("ALTER TABLE agent_tasks ADD COLUMN plan_id TEXT DEFAULT ''")
        if "step_index" not in cols:
            db.execute("ALTER TABLE agent_tasks ADD COLUMN step_index INTEGER DEFAULT -1")
        if "depends_on" not in cols:
            db.execute("ALTER TABLE agent_tasks ADD COLUMN depends_on TEXT DEFAULT '[]'")
        if "is_plan_root" not in cols:
            db.execute("ALTER TABLE agent_tasks ADD COLUMN is_plan_root INTEGER DEFAULT 0")
        # Index may not exist in old schemas
        idxs = {r[1] for r in db.execute("PRAGMA index_list(agent_tasks)").fetchall()}
        if "agent_tasks_plan" not in idxs:
            db.execute("CREATE INDEX agent_tasks_plan ON agent_tasks(plan_id, step_index)")

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
    def _decode(row) -> AgentTask | None:
        if row is None:
            return None
        checkpoint = json.loads(row["checkpoint_json"]) if row["checkpoint_json"] else None
        depends_on = json.loads(row["depends_on"]) if row["depends_on"] else []
        return AgentTask(
            id=row["id"], goal=row["goal"], status=row["status"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            attempts=row["attempts"], steps=row["steps"], checkpoint=checkpoint,
            result=row["result"], error=row["error"], idempotency_key=row["idempotency_key"],
            plan_id=row["plan_id"], step_index=row["step_index"],
            depends_on=depends_on, is_plan_root=bool(row["is_plan_root"]),
        )

    def _event(self, db, task_id: str, event: str, detail: dict | None = None) -> None:
        db.execute(
            "INSERT INTO agent_task_events(task_id,at,event,detail_json) VALUES(?,?,?,?)",
            (task_id, time.time(), event, json.dumps(detail or {}, ensure_ascii=False)),
        )

    def create(self, goal: str, *, idempotency_key: str = "") -> AgentTask:
        goal = str(goal or "").strip()
        if not goal:
            raise ValueError("task goal must not be empty")
        if len(goal) > 20_000:
            raise ValueError("task goal must be at most 20,000 characters")
        now = time.time()
        task_id = uuid.uuid4().hex
        with self._lock, self._connection() as db:
            if idempotency_key:
                existing = db.execute(
                    "SELECT * FROM agent_tasks WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                if existing:
                    return self._decode(existing)
            db.execute(
                "INSERT INTO agent_tasks(id,goal,status,created_at,updated_at,idempotency_key) "
                "VALUES(?,?,'queued',?,?,?)",
                (task_id, goal, now, now, idempotency_key),
            )
            self._event(db, task_id, "queued", {"goal": goal})
            row = db.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
        return self._decode(row)

    def get(self, task_id: str) -> AgentTask | None:
        with self._lock, self._connection() as db:
            return self._decode(db.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone())

    def list(self, *, limit: int = 20, include_terminal: bool = True) -> list[AgentTask]:
        limit = max(1, min(int(limit), 100))
        query = "SELECT * FROM agent_tasks"
        args: tuple = ()
        if not include_terminal:
            query += " WHERE status NOT IN ('completed','failed','cancelled')"
        query += " ORDER BY created_at DESC LIMIT ?"
        args += (limit,)
        with self._lock, self._connection() as db:
            return [self._decode(r) for r in db.execute(query, args).fetchall()]

    def events(self, task_id: str, *, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        with self._lock, self._connection() as db:
            rows = db.execute(
                "SELECT at,event,detail_json FROM agent_task_events WHERE task_id=? ORDER BY seq DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        return [{"at": r["at"], "event": r["event"], "detail": json.loads(r["detail_json"])}
                for r in reversed(rows)]

    def claim_next(self, *, max_attempts: int, exclude_ids=()) -> AgentTask | None:
        excluded = tuple(str(item) for item in exclude_ids)
        exclusion = " AND id NOT IN (" + ",".join("?" for _ in excluded) + ")" if excluded else ""
        params = (max_attempts, *excluded)
        with self._lock, self._connection() as db:
            row = db.execute(
                """SELECT * FROM agent_tasks
                   WHERE status='queued' AND attempts < ?"""
                + exclusion
                + """ AND (depends_on='[]' OR depends_on IS NULL OR NOT EXISTS (
                       SELECT 1 FROM agent_tasks d
                       WHERE d.id IN (SELECT value FROM json_each(agent_tasks.depends_on))
                       AND d.status != 'completed'
                   ))
                   ORDER BY is_plan_root, step_index, created_at LIMIT 1""",
                params,
            ).fetchone()
            if row is None:
                return None
            now = time.time()
            db.execute(
                "UPDATE agent_tasks SET status='running', attempts=attempts+1, updated_at=? WHERE id=? AND status='queued'",
                (now, row["id"]),
            )
            self._event(db, row["id"], "started", {"attempt": row["attempts"] + 1})
            claimed = db.execute("SELECT * FROM agent_tasks WHERE id=?", (row["id"],)).fetchone()
        return self._decode(claimed)

    def save_step(self, task_id: str, *, checkpoint: dict | None, result: str = "") -> None:
        now = time.time()
        encoded = json.dumps(checkpoint, ensure_ascii=False) if checkpoint is not None else None
        with self._lock, self._connection() as db:
            cur = db.execute(
                "UPDATE agent_tasks SET checkpoint_json=?, result=?, steps=steps+1, updated_at=? "
                "WHERE id=? AND status='running'",
                (encoded, str(result or ""), now, task_id),
            )
            if not cur.rowcount:
                raise RuntimeError("task is no longer running")
            self._event(db, task_id, "checkpoint", {"result": str(result or "")[:2000]})

    def transition(self, task_id: str, status: str, *, result: str = "", error: str = "") -> AgentTask:
        if status not in STATUSES:
            raise ValueError(f"unknown task status {status!r}")
        now = time.time()
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown task {task_id!r}")
            if status not in _ALLOWED_TRANSITIONS[row["status"]]:
                raise ValueError(f"task cannot transition from {row['status']} to {status}")
            db.execute(
                "UPDATE agent_tasks SET status=?, result=CASE WHEN ?<>'' THEN ? ELSE result END, "
                "error=?, updated_at=? WHERE id=?",
                (status, result, result, error, now, task_id),
            )
            self._event(db, task_id, status, {"result": result[:2000], "error": error[:1000]})
            row = db.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
        return self._decode(row)

    def recover_interrupted(self) -> int:
        """Recover work left by a crash; processors resume from checkpoints."""
        now = time.time()
        with self._lock, self._connection() as db:
            # A timed-out thread cannot survive process exit. Remove the live-
            # worker retry guard so the user can retry after this restart.
            timed_out = db.execute(
                "SELECT id FROM agent_tasks WHERE status='failed' AND error LIKE '%worker continues in background%'"
            ).fetchall()
            for row in timed_out:
                db.execute(
                    "UPDATE agent_tasks SET error='Timed-out worker ended with process restart; manual retry is available.', updated_at=? WHERE id=?",
                    (now, row["id"]),
                )
                self._event(db, row["id"], "timeout_recovered", {"manual_retry_available": True})
            rows = db.execute("SELECT id FROM agent_tasks WHERE status='running'").fetchall()
            for row in rows:
                db.execute(
                    "UPDATE agent_tasks SET status='queued', updated_at=?, "
                    "error='Interrupted by restart; resuming from last checkpoint.' WHERE id=?",
                    (now, row["id"]),
                )
                self._event(db, row["id"], "recovered", {"resume_from_checkpoint": True})
        return len(rows)

    def set_failure_error(self, task_id: str, error: str) -> AgentTask:
        """Update failure detail without changing the task's terminal status."""
        with self._lock, self._connection() as db:
            cur = db.execute(
                "UPDATE agent_tasks SET error=?, updated_at=? WHERE id=? AND status='failed'",
                (str(error), time.time(), task_id),
            )
            if not cur.rowcount:
                raise ValueError("task is not failed")
            self._event(db, task_id, "failure_detail", {"error": str(error)[:1000]})
            return self._decode(db.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone())

    def retry(self, task_id: str) -> AgentTask:
        task = self.get(task_id)
        if task is None:
            raise KeyError(f"unknown task {task_id!r}")
        if task.status == "failed" and "worker continues in background" in task.error:
            raise ValueError("timed-out worker is still running; wait until it finishes before retrying")
        if task.status != "failed":
            raise ValueError("only failed tasks can be resumed; confirmation-waiting tasks must be settled first")
        now = time.time()
        with self._lock, self._connection() as db:
            db.execute(
                "UPDATE agent_tasks SET status='queued', attempts=0, error='', updated_at=? WHERE id=?",
                (now, task_id),
            )
            self._event(db, task_id, "queued", {"manual_resume": True})
            return self._decode(db.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone())

    def cancel(self, task_id: str) -> AgentTask:
        task = self.get(task_id)
        if task is None:
            raise KeyError(f"unknown task {task_id!r}")
        if task.status in TERMINAL:
            raise ValueError(f"task is already {task.status}")
        return self.transition(task_id, "cancelled", error="")

    def create_plan(self, plan: "Plan") -> tuple[AgentTask, list[AgentTask]]:
        """Create root task and sub-tasks from a Plan."""
        from jarvis.agent.planner import Plan as PlanType, PlanStep
        if not isinstance(plan, PlanType):
            raise TypeError("plan must be a Plan instance")
        if not plan.steps:
            raise ValueError("plan must have at least one step")

        now = time.time()
        root_id = uuid.uuid4().hex
        plan_id = root_id  # plan_id equals root task id

        with self._lock, self._connection() as db:
            # Create root task
            root_idempotency = f"{plan.idempotency_key}:root" if plan.idempotency_key else ""
            db.execute(
                """INSERT INTO agent_tasks(id,goal,status,created_at,updated_at,idempotency_key,
                   plan_id,step_index,depends_on,is_plan_root)
                   VALUES(?,?,'queued',?,?,?,?,?,'[]',1)""",
                (root_id, plan.root_goal, now, now, root_idempotency, plan_id, -1),
            )
            self._event(db, root_id, "queued", {"goal": plan.root_goal, "is_plan_root": True})

            # First pass: create sub-tasks with empty depends_on, collect IDs
            index_to_id = {}
            subtasks_info = []
            for i, step in enumerate(plan.steps):
                task_id = uuid.uuid4().hex
                step_idempotency = f"{plan.idempotency_key}:step:{i}" if plan.idempotency_key else ""
                index_to_id[step.index] = task_id
                subtasks_info.append((task_id, step, step_idempotency))

            # Second pass: update depends_on with actual task IDs
            for task_id, step, step_idempotency in subtasks_info:
                depends_on_ids = [index_to_id[dep_idx] for dep_idx in step.depends_on]
                depends_on_json = json.dumps(depends_on_ids)

                db.execute(
                    """INSERT INTO agent_tasks(id,goal,status,created_at,updated_at,idempotency_key,
                       plan_id,step_index,depends_on,is_plan_root)
                       VALUES(?,?,'queued',?,?,?,?,?,?,0)""",
                    (task_id, step.goal, now, now, step_idempotency, plan_id, step.index, depends_on_json),
                )
                self._event(db, task_id, "queued", {"goal": step.goal, "step_index": step.index, "depends_on": depends_on_ids})

            # Build subtasks list with resolved IDs
            subtasks: list[AgentTask] = []
            for task_id, step, step_idempotency in subtasks_info:
                depends_on_ids = [index_to_id[dep_idx] for dep_idx in step.depends_on]
                subtasks.append(AgentTask(
                    id=task_id, goal=step.goal, status="queued",
                    created_at=now, updated_at=now, attempts=0, steps=0,
                    checkpoint=None, result="", error="",
                    idempotency_key=step_idempotency,
                    plan_id=plan_id, step_index=step.index,
                    depends_on=depends_on_ids, is_plan_root=False,
                ))

            # Return root task
            root_row = db.execute("SELECT * FROM agent_tasks WHERE id=?", (root_id,)).fetchone()
            root_task = self._decode(root_row)
        return root_task, subtasks

    def get_plan_tasks(self, plan_id: str) -> list[AgentTask]:
        """Return all tasks in a plan (root + sub-tasks) ordered by step_index."""
        with self._lock, self._connection() as db:
            rows = db.execute(
                "SELECT * FROM agent_tasks WHERE plan_id=? ORDER BY step_index",
                (plan_id,),
            ).fetchall()
        return [self._decode(r) for r in rows]

    def mark_deps_completed(self, completed_task_id: str) -> list[AgentTask]:
        """Find tasks waiting on completed_task_id and re-queue them if all deps met."""
        # First, get the completed task to find its plan_id
        completed = self.get(completed_task_id)
        if completed is None or not completed.plan_id:
            return []

        plan_id = completed.plan_id
        now = time.time()
        ready: list[AgentTask] = []

        with self._lock, self._connection() as db:
            # Find queued tasks in the same plan that depend on completed_task_id
            rows = db.execute(
                """SELECT * FROM agent_tasks
                   WHERE plan_id=? AND status='queued'
                   AND depends_on != '[]' AND depends_on IS NOT NULL
                   AND EXISTS (
                       SELECT 1 FROM json_each(depends_on)
                       WHERE value = ?
                   )
                   ORDER BY step_index""",
                (plan_id, completed_task_id),
            ).fetchall()

            for row in rows:
                task = self._decode(row)
                # Check if ALL dependencies are completed
                all_deps_completed = True
                for dep_id in task.depends_on:
                    dep_task = self.get(dep_id)
                    if dep_task is None or dep_task.status != "completed":
                        all_deps_completed = False
                        break

                if all_deps_completed:
                    # Task is already queued, just ensure it's claimable (it already is)
                    # We return it as "ready" - no state change needed
                    ready.append(task)

        return ready


class DurableTaskRunner:
    """Run one checkpointed processor step at a time, with strict work bounds."""

    def __init__(self, store: TaskStore, processor: Callable[[AgentTask], StepResult], *,
                 max_attempts: int = 3, max_steps: int = 20, timeout_seconds: float = 120):
        self.store = store
        self.processor = processor
        self.max_attempts = max(1, min(int(max_attempts), 10))
        self.max_steps = max(1, min(int(max_steps), 100))
        self.timeout_seconds = max(0.01, min(float(timeout_seconds), 900.0))
        self._run_lock = threading.Lock()
        self._active_steps: set[str] = set()
        self._timed_out_steps: set[str] = set()
        self._timeout_lock = threading.Lock()
        self.store.recover_interrupted()

    def run_once(self) -> AgentTask | None:
        if not self._run_lock.acquire(blocking=False):
            return None
        worker = None
        task = None
        try:
            task = self.store.claim_next(max_attempts=self.max_attempts, exclude_ids=self._active_steps)
            if task is None:
                return None
            if task.steps >= self.max_steps:
                return self.store.transition(task.id, "failed", error="maximum step limit reached")
            result_box: list[StepResult] = []
            error_box: list[BaseException] = []

            def invoke():
                try:
                    value = self.processor(task)
                    result_box.append(value if isinstance(value, StepResult) else StepResult(done=True, result=str(value)))
                except BaseException as exc:
                    error_box.append(exc)

            self._active_steps.add(task.id)

            def run_and_release():
                try:
                    invoke()
                finally:
                    with self._timeout_lock:
                        if task.id in self._timed_out_steps:
                            try:
                                self.store.set_failure_error(
                                    task.id, "Timed-out worker finished; manual retry is available."
                                )
                            except (KeyError, ValueError):
                                pass
                            self._timed_out_steps.discard(task.id)
                        self._active_steps.discard(task.id)

            worker = threading.Thread(target=run_and_release, name=f"jarvis-task-{task.id[:8]}", daemon=True)
            worker.start()
            worker.join(self.timeout_seconds)
            with self._timeout_lock:
                if worker.is_alive():
                    # Python cannot safely stop a running processor thread.
                    # Keep it failed and block manual retries until it exits.
                    self._timed_out_steps.add(task.id)
                    return self.store.transition(
                        task.id, "failed",
                        error=f"TimeoutError: task step exceeded {self.timeout_seconds:g}s; worker continues in background",
                    )
            if error_box:
                err = error_box[0]
                if task.attempts < self.max_attempts:
                    return self.store.transition(task.id, "queued", error=f"{type(err).__name__}: {err}")
                return self.store.transition(task.id, "failed", error=f"{type(err).__name__}: {err}")
            result = result_box[0]
            self.store.save_step(task.id, checkpoint=result.checkpoint, result=result.result)
            if result.error:
                if task.attempts < self.max_attempts:
                    return self.store.transition(task.id, "queued", error=result.error)
                return self.store.transition(task.id, "failed", error=result.error)
            if result.waiting_confirmation:
                return self.store.transition(task.id, "waiting_confirmation", result=result.result)
            if result.done:
                return self.store.transition(task.id, "completed", result=result.result)
            updated = self.store.get(task.id)
            if updated and updated.steps >= self.max_steps:
                return self.store.transition(task.id, "failed", error="maximum step limit reached")
            return self.store.transition(task.id, "queued", result=result.result)
        finally:
            if worker is None or not worker.is_alive():
                if task is not None:
                    self._active_steps.discard(task.id)
                    self._timed_out_steps.discard(task.id)
            self._run_lock.release()

    def drain(self, *, max_tasks: int = 10) -> list[AgentTask]:
        """Process at most max_tasks checkpoints; never spin forever on retries."""
        max_tasks = max(1, min(int(max_tasks), 100))
        completed = []
        for _ in range(max_tasks):
            task = self.run_once()
            if task is None:
                break
            completed.append(task)
        return completed


__all__ = ["AgentTask", "StepResult", "TaskStore", "DurableTaskRunner", "STATUSES",
           "configure_task_store", "configured_task_store"]
