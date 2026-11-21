# Task 3: Extend TaskStore for Plan Operations

## Context
This is Task 3 of 7. Adds plan-level operations to TaskStore: create_plan, get_plan_tasks, mark_deps_completed, and modifies claim_next to honor dependencies.

## Files to Modify
- `jarvis/agent/task_runner.py` (add methods to TaskStore class)
- `test_task_runner.py` (extend with new tests)

## Requirements (from plan verbatim)

### New Methods

```python
def create_plan(self, plan: Plan) -> tuple[AgentTask, list[AgentTask]]:
    """Create root task and sub-tasks from a Plan."""
    # Root: is_plan_root=1, step_index=-1, plan_id=root.id
    # Sub-tasks: plan_id=root.id, step_index=0..N-1, depends_on=JSON array of sibling task IDs
    # Sub-task idempotency_key = f"{plan.idempotency_key}:step:{index}" if plan has key
    pass

def get_plan_tasks(self, plan_id: str) -> list[AgentTask]:
    """Return all tasks in a plan (root + sub-tasks) ordered by step_index."""
    pass

def mark_deps_completed(self, completed_task_id: str) -> list[AgentTask]:
    """Find tasks waiting on completed_task_id and re-queue them if all deps met."""
    # Query queued tasks in same plan that depend on completed_task_id
    # Check if ALL their dependencies are completed
    # If so, re-queue them (they're already queued, just ensure they're claimable)
    pass
```

### Modified `claim_next`
Skip tasks whose `depends_on` references tasks that are not `completed`:
```sql
SELECT * FROM agent_tasks 
WHERE status='queued' AND attempts < ? 
AND (depends_on='[]' OR depends_on IS NULL OR NOT EXISTS (
  SELECT 1 FROM agent_tasks d 
  WHERE d.id IN (SELECT value FROM json_each(agent_tasks.depends_on)) 
  AND d.status != 'completed'
)) ORDER BY created_at LIMIT 1
```

## Tests to Pass (exact)

```python
def test_create_plan_creates_root_and_subtasks(tmp_path):
    from jarvis.agent.task_runner import TaskStore, Plan, PlanStep
    store = TaskStore(tmp_path / "tasks.db")
    plan = Plan(root_goal="Test plan", steps=[
        PlanStep(index=0, goal="Step A", depends_on=[]),
        PlanStep(index=1, goal="Step B", depends_on=[0]),
    ], idempotency_key="plan-key-1")
    root, subtasks = store.create_plan(plan)
    assert root.is_plan_root is True
    assert root.plan_id == root.id
    assert root.step_index == -1
    assert len(subtasks) == 2
    for i, st in enumerate(subtasks):
        assert st.plan_id == root.id
        assert st.step_index == i
        assert json.loads(st.depends_on) == plan.steps[i].depends_on
        assert st.idempotency_key == f"plan-key-1:step:{i}"

def test_get_plan_tasks_returns_ordered(tmp_path):
    from jarvis.agent.task_runner import TaskStore, Plan, PlanStep
    store = TaskStore(tmp_path / "tasks.db")
    plan = Plan(root_goal="Test", steps=[
        PlanStep(index=0, goal="A", depends_on=[]),
        PlanStep(index=1, goal="B", depends_on=[0]),
    ], idempotency_key="k")
    root, _ = store.create_plan(plan)
    tasks = store.get_plan_tasks(root.id)
    assert len(tasks) == 3  # root + 2 subtasks
    assert tasks[0].is_plan_root
    assert [t.step_index for t in tasks[1:]] == [0, 1]

def test_mark_deps_completed_requeues_waiting_tasks(tmp_path):
    from jarvis.agent.task_runner import TaskStore, Plan, PlanStep
    store = TaskStore(tmp_path / "tasks.db")
    plan = Plan(root_goal="Test", steps=[
        PlanStep(index=0, goal="A", depends_on=[]),
        PlanStep(index=1, goal="B", depends_on=[0]),
    ], idempotency_key="k")
    root, subs = store.create_plan(plan)
    # Complete step 0
    store.transition(subs[0].id, "completed", result="done")
    # Step 1 should now be claimable (was blocked by deps)
    ready = store.mark_deps_completed(subs[0].id)
    assert len(ready) == 1
    assert ready[0].id == subs[1].id
    # Note: mark_deps_completed returns tasks that became claimable
```

Additional test for `claim_next` honoring deps:
```python
def test_claim_next_skips_blocked_tasks(tmp_path):
    from jarvis.agent.task_runner import TaskStore, Plan, PlanStep
    store = TaskStore(tmp_path / "tasks.db")
    plan = Plan(root_goal="Test", steps=[
        PlanStep(index=0, goal="A", depends_on=[]),
        PlanStep(index=1, goal="B", depends_on=[0]),
    ], idempotency_key="k")
    root, subs = store.create_plan(plan)
    # Step 1 should not be claimed while step 0 is pending
    claimed = store.claim_next(max_attempts=3)
    assert claimed is not None
    assert claimed.id == subs[0].id  # step 0 claimed first
    # Complete step 0
    store.transition(subs[0].id, "completed", result="done")
    # Now step 1 should be claimable
    claimed2 = store.claim_next(max_attempts=3)
    assert claimed2 is not None
    assert claimed2.id == subs[1].id
```

Run: `pytest test_task_runner.py -v` (all 15+ tests should pass)

## Global Constraints
- Python 3.11+; no new dependencies
- All checkpoints/results JSON-serializable
- Must work in both Telegram (`Brain`) and Orb (`AgentContext`) hosts

## Interfaces from Earlier Tasks
- `Plan` and `PlanStep` from `jarvis.agent.planner` (Task 2)
- `TaskStore` with new columns from Task 1
- `AgentTask` dataclass with `plan_id`, `step_index`, `depends_on`, `is_plan_root`

## Report File
Write full report to: `C:/Users/HP/Desktop/Projects/JARVIS/.superpowers/sdd/2026-11-19-planner-subtask-queue/task-3-report.md`

Return only: status, commits, one-line test summary, concerns.