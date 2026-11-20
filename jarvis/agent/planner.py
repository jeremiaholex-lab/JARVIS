"""Planner module: LLM-driven goal decomposition into a validated DAG of sub-tasks."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Callable


@dataclass(slots=True)
class PlanStep:
    index: int
    goal: str
    depends_on: list[int] = field(default_factory=list)
    max_steps: int = 4
    max_attempts: int = 2
    role: str = "general"


@dataclass(slots=True)
class Plan:
    root_goal: str
    steps: list[PlanStep]
    idempotency_key: str = ""


PLANNER_PROMPT = """Decompose the user's goal into a sequence of bounded sub-tasks.
Each sub-task must be completable with read-only tools in at most 4 steps.
Return JSON only: {{"steps": [{{"index": 0, "goal": "...", "depends_on": [], "role": "researcher"}}, ...]}}
- Max {max_subtasks} steps
- depends_on contains indices of prerequisite steps (must form a DAG)
- No circular dependencies
- Steps execute sequentially in topological order
- role picks which specialist runs the step, one of:
{roles}
  Use "researcher" to find and cite facts, "coder" to compute or parse with
  sandboxed Python, "reviewer" to check work already done, "general" when no
  specialist fits. A reviewer step should depend on the step it reviews.
Goal: {goal}"""


def decompose_goal(goal: str, max_subtasks: int, call_engine: Callable,
                   idempotency_key: str = "") -> Plan:
    """Decompose a goal into a validated Plan of sub-tasks.

    Args:
        goal: The user's high-level goal to decompose.
        max_subtasks: Maximum number of sub-tasks (1-10, default 5).
        call_engine: Callable(engine: str, msgs: list[dict], tools: list[dict] | None) -> dict
        idempotency_key: Optional request id, carried onto the Plan so the
            store can dedupe a repeated enqueue.

    Returns:
        Plan with validated steps forming a DAG.

    Raises:
        ValueError: If the LLM response is invalid JSON, missing required fields,
                    exceeds max_subtasks, has invalid dependencies, or contains cycles.
    """
    if not isinstance(max_subtasks, int) or max_subtasks < 1 or max_subtasks > 10:
        raise ValueError("max_subtasks must be an integer between 1 and 10")

    from .roles import normalise_role, role_names

    role_lines = "\n".join(
        f'  - "{name}": {get_role_description(name)}' for name in role_names()
    )
    prompt = PLANNER_PROMPT.format(goal=goal, max_subtasks=max_subtasks, roles=role_lines)

    response = call_engine("local", [{"role": "user", "content": prompt}], tools=None)

    content = response.get("content", "")
    if not content:
        raise ValueError("Empty response from engine")

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON response: {e}")

    if "steps" not in parsed or not isinstance(parsed["steps"], list):
        raise ValueError("Response must contain 'steps' list")

    raw_steps = parsed["steps"]

    # Truncate to max_subtasks
    if len(raw_steps) > max_subtasks:
        raw_steps = raw_steps[:max_subtasks]

    # Validate and build PlanStep objects
    steps: list[PlanStep] = []
    indices_seen = set()

    for i, step_data in enumerate(raw_steps):
        if not isinstance(step_data, dict):
            raise ValueError(f"Step {i} must be an object")

        index = step_data.get("index")
        if not isinstance(index, int):
            raise ValueError(f"Step {i} missing or invalid 'index'")

        if index in indices_seen:
            raise ValueError(f"Duplicate step index: {index}")
        indices_seen.add(index)

        goal_text = step_data.get("goal")
        if not isinstance(goal_text, str) or not goal_text.strip():
            raise ValueError(f"Step {index} missing or empty 'goal'")

        depends_on = step_data.get("depends_on", [])
        if not isinstance(depends_on, list) or not all(isinstance(d, int) for d in depends_on):
            raise ValueError(f"Step {index} 'depends_on' must be a list of integers")

        # An unknown or missing role degrades to "general" rather than failing
        # the plan: a hallucinated role should cost a worse tool set, not the
        # whole task.
        role = normalise_role(step_data.get("role"))

        steps.append(PlanStep(
            index=index,
            goal=goal_text.strip(),
            depends_on=depends_on,
            role=role,
        ))

    # Validate all depends_on indices exist
    valid_indices = {s.index for s in steps}
    for step in steps:
        for dep in step.depends_on:
            if dep not in valid_indices:
                raise ValueError(f"Step {step.index} has invalid dependency: {dep}")

    # Validate no circular dependencies (Kahn's algorithm)
    _validate_dag(steps)

    return Plan(root_goal=goal, steps=steps, idempotency_key=idempotency_key)


def get_role_description(name: str) -> str:
    """One-line description of a role, for the planner prompt."""
    from .roles import get_role

    return get_role(name).description


def _validate_dag(steps: list[PlanStep]) -> None:
    """Validate that the dependency graph has no cycles using Kahn's algorithm.

    Args:
        steps: List of PlanStep objects to validate.

    Raises:
        ValueError: If a cycle is detected.
    """
    # Build adjacency list and in-degree count
    adj = {s.index: [] for s in steps}
    in_degree = {s.index: 0 for s in steps}

    for step in steps:
        for dep in step.depends_on:
            adj[dep].append(step.index)
            in_degree[step.index] += 1

    # Kahn's algorithm
    queue = deque([idx for idx, deg in in_degree.items() if deg == 0])
    visited = 0

    while queue:
        current = queue.popleft()
        visited += 1
        for neighbor in adj[current]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if visited != len(steps):
        raise ValueError("circular dependency")