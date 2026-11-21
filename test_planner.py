"""Tests for planner module."""

import json
import pytest
from jarvis.agent.planner import decompose_goal, Plan


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

    with pytest.raises(ValueError, match="circular dependency"):
        decompose_goal("Circular", max_subtasks=5, call_engine=mock_engine)


def test_decompose_rejects_missing_dependency():
    def mock_engine(engine, msgs, tools):
        return {"content": json.dumps({"steps": [
            {"index": 0, "goal": "A", "depends_on": [2]},
            {"index": 1, "goal": "B", "depends_on": [0]},
        ]})}

    with pytest.raises(ValueError, match="invalid dependency"):
        decompose_goal("Bad DAG", max_subtasks=5, call_engine=mock_engine)