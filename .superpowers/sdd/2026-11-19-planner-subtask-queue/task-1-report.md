# Task 1 Report: Extend TaskStore Schema

## Status
✅ Completed

## Commits
- No commits yet (working directory changes only)

## Test Summary
All 11 tests pass, including the new `test_task_store_has_plan_columns` test that verifies the four new columns (`plan_id`, `step_index`, `depends_on`, `is_plan_root`) and the `agent_tasks_plan` index exist in the database schema.

## Changes Made

### 1. `jarvis/agent/task_runner.py`
- Added `field` import from `dataclasses`
- Extended `AgentTask` dataclass with four new fields:
  - `plan_id: str = ""`
  - `step_index: int = -1`
  - `depends_on: list[str] = field(default_factory=list)`
  - `is_plan_root: bool = False`
- Added schema migration in `TaskStore.__init__`:
  - `ALTER TABLE` statements for the four new columns with defaults
  - `CREATE INDEX` for `agent_tasks_plan` on `(plan_id, step_index)`
  - Added `_migrate_schema` method for handling existing databases gracefully
- Updated `_decode` static method to parse:
  - `depends_on` JSON array
  - `is_plan_root` integer to boolean conversion

### 2. `test_task_runner.py`
- Added `test_task_store_has_plan_columns` test as specified in the requirements

## Concerns
- The migration adds columns directly in the `CREATE TABLE` statement via `ALTER TABLE` which works for new databases. For existing databases, the `_migrate_schema` method handles it after table creation. This dual approach ensures compatibility but runs `ALTER TABLE` twice on fresh databases (harmless but slightly redundant).
- SQLite doesn't support `ADD COLUMN IF NOT EXISTS`, so the migration guards are necessary for existing databases.