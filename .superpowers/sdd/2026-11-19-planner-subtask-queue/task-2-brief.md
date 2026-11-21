# Task 2: Implement Planner (`decompose_goal`)

## Context
This is Task 2 of 7. Implements the LLM-driven planner that decomposes a goal into a validated DAG of sub-tasks.

## Files to Create
- `jarvis/agent/planner.py` (new)
- `test_planner.py` (new)

## Requirements (from plan verbatim)

### Dataclasses
```python
@dataclass(slots=True)
class PlanStep:
    index: int
    goal: str
    depends_on: list[int] = field(default_factory=list)
    max_steps: int = 4
    max_attempts: int = 2

@dataclass(slots=True)
class Plan:
    root_goal: str
    steps: list[PlanStep]
    idempotency_key: str = ""
```

### Function Signature
```python
def decompose_goal(goal: str, max_subtasks: int, call_engine: Callable) -> Plan:
```

### Behavior
1. Call `call_engine("local", [{"role": "user", "content": prompt}], tools=None)` with planner prompt
2. Parse JSON response: `{"steps": [{"index": 0, "goal": "...", "depends_on": []}, ...]}`
3. Validate:
   - Max `max_subtasks` steps (truncate if more)
   - All `depends_on` indices exist
   - No circular dependencies (Kahn's algorithm)
   - Forward references allowed (dependencies on higher indices)
4. Raise `ValueError` on validation failure
5. Return `Plan` with truncated/validated steps

### Planner Prompt (exact)
```
Decompose the user's goal into a sequence of bounded sub-tasks.
Each sub-task must be completable with read-only tools in at most 4 steps.
Return JSON only: {"steps": [{"index": 0, "goal": "...", "depends_on": []}, ...]}
- Max {max_subtasks} steps
- depends_on contains indices of prerequisite steps (must form a DAG)
- No circular dependencies
- Steps execute sequentially in topological order
Goal: {goal}
```

## Tests to Pass (exact)

```python
def test_decompose_simple_goal_returns_plan():
    from jarvis.agent.planner import decompose_goal, Plan
    def mock_engine(engine, msgs, tools):
        return {"content": json.dumps({"steps": [
            {"index": 0, "goal": "Summarize project deadlines", "depends_on": []},
            {"index": 1, "goal": "Draft weekly report", "depends_on": [0]},
            {"index": 2, "goal": "Email report to team", "depends_on": [1]},
        ]})}
    plan = decompose_goal("Summarize deadlines, draft report, email it", max_subtasks=5, call_engine=mock_engine)
    assert isinstance(plan, Plan)
    assert len(plan.steps) == 3
    assert plan.steps[0].depends_on == []
    assert plan.steps[1].depends_on == [0]
    assert plan.steps[2].depends_on == [1]

def test_decompose_truncates_at_max_subtasks():
    def mock_engine(engine, msgs, tools):
        return {"content": json.dumps({"steps": [
            {"index": i, "goal": f"Step {i}", "depends_on": [i-1] if i > 0 else []}
            for i in range(8)
        ]})}
    plan = decompose_goal("Many steps", max_subtasks=5, call_engine=mock_engine)
    assert len(plan.steps) == 5  # truncated

def test_decompose_rejects_circular_dependency():
    def mock_engine(engine, msgs, tools):
        return {"content": json.dumps({"steps": [
            {"index": 0, "goal": "A", "depends_on": [1]},
            {"index": 1, "goal": "B", "depends_on": [0]},
        ]})}
    import pytest
    with pytest.raises(ValueError, match="circular dependency"):
        decompose_goal("Circular", max_subtasks=5, call_engine=mock_engine)

def test_decompose_validates_dag():
    def mock_engine(engine, msgs, tools):
        return {"content": json.dumps({"steps": [
            {"index": 0, "goal": "A", "depends_on": [2]},  # forward ref
            {"index": 1, "goal": "B", "depends_on": [0]},
            {"index": 2, "goal": "C", "depends_on": [1]},
        ]})}
    import pytest
    with pytest.raises(ValueError, match="invalid dependency"):
        decompose_goal("Bad DAG", max_subtasks=5, call_engine=mock_engine)
```

Run: `pytest test_planner.py -v`

## Global Constraints
- Python 3.11+; no new dependencies
- Max sub-tasks per plan: 5 (configurable, max 10)
- All checkpoints/results JSON-serializable

## Report File
Write full report to: `C:/Users/HP/Desktop/Projects/JARVIS/.superpowers/sdd/2026-11-19-planner-subtask-queue/task-2-report.md`

Return only: status, commits, one-line test summary, concerns.