# Planner + Sub-Task Queue Design

> **Status:** Approved for implementation

## Intent

JARVIS currently runs background tasks as single bounded turns (max 4 tool rounds, 8 steps). Complex user goals like *"summarize all project deadlines, draft a weekly report, and email it"* cannot be expressed — they require decomposition into dependent sub-tasks, each with its own checkpoint and retry policy.

This design adds a **planner** that decomposes a high-level goal into a **task graph** (DAG), and a **sub-task queue** that executes nodes in topological order, respecting dependencies and propagating results.

---

## Goals

1. **Decompose** a user goal into ordered sub-tasks with explicit dependencies
2. **Execute** sub-tasks via the existing `DurableTaskRunner`, one step per node
3. **Propagate** each sub-task's result/checkpoint to its dependents
4. **Bound** the entire plan: max total steps, max wall time, max sub-tasks
5. **Persist** the plan so restarts resume from the last completed node
6. **Expose** via a single tool: `jarvis_plan_and_enqueue(goal, max_subtasks=5)`

## Non-Goals

- Dynamic replanning mid-execution (v2)
- Parallel execution of independent nodes (v2 — current runner is single-threaded)
- Human-in-the-loop approval per sub-task (v2 — confirmation stays at plan level)
- Cross-channel plan sharing (Telegram ↔ Orb) — out of scope for this plan

## Anti-Goals

- Do not replace `DurableTaskRunner` — it remains the unit of execution
- Do not add a new DB table per plan — reuse `agent_tasks` with a `plan_id` + `step_index`
- Do not expose planner internals as tools — single entry point

---

## Architecture

### Data Model (extends `agent_tasks`)

```sql
-- Existing columns reused:
-- id, goal, status, attempts, steps, checkpoint_json, result, error, idempotency_key

-- New columns (added via ALTER TABLE):
ALTER TABLE agent_tasks ADD COLUMN plan_id TEXT DEFAULT '';
ALTER TABLE agent_tasks ADD COLUMN step_index INTEGER DEFAULT -1;
ALTER TABLE agent_tasks ADD COLUMN depends_on TEXT DEFAULT '[]';  -- JSON array of task IDs
ALTER TABLE agent_tasks ADD COLUMN is_plan_root INTEGER DEFAULT 0;

CREATE INDEX IF NOT EXISTS agent_tasks_plan ON agent_tasks(plan_id, step_index);
```

- **Plan root task**: `is_plan_root=1`, `step_index=-1`, `goal` = user's high-level goal
- **Sub-tasks**: `plan_id` = root's `id`, `step_index` = 0..N-1, `depends_on` = JSON array of sibling task IDs
- All tasks in a plan share the same `idempotency_key` namespace (root's key + `:step:N`)

### Planner Interface

```python
# jarvis/agent/planner.py

@dataclass(slots=True)
class PlanStep:
    index: int
    goal: str
    depends_on: list[int]  # indices of prerequisite steps
    max_steps: int = 4     # tighter bound per sub-task
    max_attempts: int = 2

@dataclass(slots=True)
class Plan:
    root_goal: str
    steps: list[PlanStep]
    idempotency_key: str

def decompose_goal(goal: str, max_subtasks: int = 5) -> Plan:
    """LLM-driven decomposition. Returns a validated Plan or raises ValueError."""
    ...
```

### Execution Flow

```
User calls jarvis_plan_and_enqueue("Summarize deadlines, draft report, email")
       │
       ▼
decompose_goal() → Plan with 3 steps
       │
       ▼
Create root task (is_plan_root=1, status=queued)
Create sub-tasks (status=queued, depends_on=[...], plan_id=root.id)
       │
       ▼
DurableTaskRunner picks root task → processor runs planner (no-op, just advances)
       │
       ▼
Sub-task 0 (no deps) → queued → running → checkpoint/completed
       │
       ▼
Sub-task 1 (depends_on=[0]) → queued when step 0 completes
       │
       ▼
Sub-task 2 (depends_on=[1]) → queued when step 1 completes
       │
       ▼
Root task transitions to completed with aggregated result
```

### Dependency Resolution

- `TaskStore.claim_next()` skips tasks whose `depends_on` tasks are not `completed`
- When a task completes, `transition()` checks if any waiting tasks now have all deps met → re-queues them
- Root task's `processor` is a no-op that just marks the plan "in progress" and returns `done=True` immediately

### Bounds

| Limit | Value | Enforced By |
|-------|-------|-------------|
| Max sub-tasks per plan | 5 (configurable, max 10) | `decompose_goal` validation |
| Max steps per sub-task | 4 | `PlanStep.max_steps` |
| Max attempts per sub-task | 2 | `PlanStep.max_attempts` |
| Max wall time per plan | 600s | `DurableTaskRunner.timeout_seconds` on root |
| Max total steps per plan | 20 | Root task `max_steps` |

---

## Tool Signature

```python
# In jarvis/agent/tools.py

def _plan_and_enqueue(goal: str, max_subtasks: int = 5, idempotency_key: str = "") -> str:
    """Decompose a complex goal into a bounded plan and enqueue it for background execution.
    Returns the plan root task_id. Sub-tasks run sequentially with read-only tools.
    Use jarvis_task_status with the root task_id to track overall progress."""
    ...
```

- Tier: **READ** (enqueues only; mutations require separate confirmation)
- Available in both Telegram (`Brain`) and Orb (`AgentContext`)

---

## UI / API

- **Orb REST**: Reuses `/api/tasks` — root task appears in list; sub-tasks filterable by `plan_id`
- **Orb WebSocket**: `plan_progress` event emitted on each sub-task completion
- **Telegram**: Same `jarvis_task_status` tool shows plan root + sub-tasks

---

## Constraints

- Python 3.11+
- No new dependencies (uses existing `DurableTaskRunner`, `TaskStore`, `run_turn`)
- LLM call for decomposition uses existing `call_engine` with `max_tool_rounds=0` (no tools)
- All checkpoints/results JSON-serializable (existing constraint)

---

## Review Focus

| Input / Condition | Expected Behavior |
|-------------------|-------------------|
| Goal decomposes to > `max_subtasks` steps | Planner merges/truncates; never exceeds bound |
| Sub-task fails all retries | Plan root transitions to `failed` with aggregated error; remaining sub-tasks cancelled |
| Process restarts mid-plan | `recover_interrupted()` re-queues root; sub-tasks with `completed` deps resume |
| Circular dependency in planner output | `decompose_goal` validates DAG; raises `ValueError` on cycle |
| Idempotency key collision on sub-task | Sub-task keys are `f"{root_key}:step:{index}"` — unique per plan |
| Sub-task result too large for checkpoint | Truncated at 2000 chars (existing `TaskStore` limit) |
| Root task timeout while sub-tasks running | Root fails with "plan timeout"; sub-tasks continue but blocked from retry until they finish |