# Task 1: Extend TaskStore Schema

## Context
This is Task 1 of 7 in the Planner + Sub-Task Queue plan. It adds four new columns to the `agent_tasks` table and extends the `AgentTask` dataclass.

## Files to Modify
- `jarvis/agent/task_runner.py` lines 74-104 (schema creation in `TaskStore.__init__`)
- `jarvis/agent/task_runner.py` lines 33-46 (`AgentTask` dataclass)
- `jarvis/agent/task_runner.py` lines 120-130 (`_decode` method)

## Requirements (from plan verbatim)

### Schema Changes
Add to `executescript` in `TaskStore.__init__`:
```sql
ALTER TABLE agent_tasks ADD COLUMN plan_id TEXT DEFAULT '';
ALTER TABLE agent_tasks ADD COLUMN step_index INTEGER DEFAULT -1;
ALTER TABLE agent_tasks ADD COLUMN depends_on TEXT DEFAULT '[]';
ALTER TABLE agent_tasks ADD COLUMN is_plan_root INTEGER DEFAULT 0;
CREATE INDEX IF NOT EXISTS agent_tasks_plan ON agent_tasks(plan_id, step_index);
```

### AgentTask Dataclass
Add fields:
```python
plan_id: str = ""
step_index: int = -1
depends_on: list[str] = field(default_factory=list)
is_plan_root: bool = False
```

### _decode Method
Parse `depends_on` JSON and `is_plan_root` int→bool.

## Test to Pass
```python
def test_task_store_has_plan_columns(tmp_path):
    from jarvis.agent.task_runner import TaskStore
    store = TaskStore(tmp_path / "tasks.db")
    import sqlite3
    with sqlite3.connect(str(tmp_path / "tasks.db")) as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(agent_tasks)").fetchall()}
        assert "plan_id" in cols
        assert "step_index" in cols
        assert "depends_on" in cols
        assert "is_plan_root" in cols
        idxs = {r[1] for r in db.execute("PRAGMA index_list(agent_tasks)").fetchall()}
        assert "agent_tasks_plan" in idxs
```

Run: `pytest test_task_runner.py::test_task_store_has_plan_columns -v`

## Global Constraints
- Python 3.11+; no new dependencies
- All checkpoints/results JSON-serializable
- Must work in both Telegram (`Brain`) and Orb (`AgentContext`) hosts

## Report File
Write full report to: `C:/Users/HP/Desktop/Projects/JARVIS/.superpowers/sdd/2026-11-19-planner-subtask-queue/task-1-report.md`

Return only: status, commits, one-line test summary, concerns.