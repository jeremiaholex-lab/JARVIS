from jarvis.agent.task_runner import DurableTaskRunner, StepResult, TaskStore
from jarvis.agent.planner import Plan, PlanStep
import json


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


def test_timeout_does_not_start_a_duplicate_retry(tmp_path):
    import time

    store = TaskStore(tmp_path / "tasks.db")
    task = store.create("Must not duplicate a slow step")
    calls = []

    def slow(_):
        calls.append(time.monotonic())
        time.sleep(0.15)
        return StepResult(done=True, result="finished")

    runner = DurableTaskRunner(store, slow, max_attempts=3, timeout_seconds=0.03)
    result = runner.run_once()
    assert result.status == "failed"
    assert "worker continues" in result.error
    assert store.claim_next(max_attempts=3, exclude_ids=runner._active_steps) is None
    time.sleep(0.2)
    assert len(calls) == 1


def test_restart_releases_retry_guard_for_orphaned_timeout(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create("Timed out before restart")
    store.claim_next(max_attempts=3)
    failed = store.transition(
        task.id, "failed", error="TimeoutError: worker continues in background"
    )
    assert "worker continues" in failed.error

    DurableTaskRunner(store, lambda _: StepResult(done=True, result="Recovered"))
    assert "manual retry is available" in store.get(task.id).error
    assert store.retry(task.id).status == "queued"


def test_task_persists_across_store_instances(tmp_path):
    path = tmp_path / "tasks.db"
    task = TaskStore(path).create("Summarize today's meetings")
    restored = TaskStore(path).get(task.id)
    assert restored is not None
    assert restored.goal == task.goal
    assert restored.status == "queued"
    assert TaskStore(path).events(task.id)[0]["event"] == "queued"


def test_idempotency_key_does_not_duplicate_tasks(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    first = store.create("Check calendar", idempotency_key="request-1")
    second = store.create("Check calendar", idempotency_key="request-1")
    assert second.id == first.id
    assert len(store.list()) == 1


def test_runner_checkpoints_then_completes(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create("Summarize project")
    calls = []

    def process(current):
        calls.append(current)
        return StepResult(done=True, checkpoint={"tools_used": ["jarvis_projects_read"]}, result="Done")

    result = DurableTaskRunner(store, process).run_once()
    assert result.status == "completed"
    persisted = store.get(task.id)
    assert persisted.steps == 1
    assert persisted.checkpoint == {"tools_used": ["jarvis_projects_read"]}
    assert persisted.result == "Done"
    assert [event["event"] for event in store.events(task.id)] == ["queued", "started", "checkpoint", "completed"]
    assert len(calls) == 1


def test_interrupted_running_task_is_recovered_from_checkpoint(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create("Continue after restart")
    running = store.claim_next(max_attempts=3)
    assert running.status == "running"
    store.save_step(task.id, checkpoint={"phase": 1}, result="Checkpoint one")

    restarted = DurableTaskRunner(store, lambda current: StepResult(
        done=True, checkpoint={"phase": 2}, result="Resumed"
    ))
    result = restarted.run_once()
    assert result.status == "completed"
    assert result.steps == 2
    assert result.checkpoint == {"phase": 2}
    assert any(event["event"] == "recovered" for event in store.events(task.id))


def test_retry_is_bounded_and_failure_is_persistent(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create("Fail safely")
    runner = DurableTaskRunner(store, lambda _: StepResult(error="temporary failure"), max_attempts=2)
    first = runner.run_once()
    assert first.status == "queued"
    failed = runner.run_once()
    assert failed.status == "failed"
    assert failed.attempts == 2
    assert failed.error == "temporary failure"


def test_waiting_confirmation_is_a_stable_nonexecuting_state(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create("Propose a file edit")
    runner = DurableTaskRunner(store, lambda _: StepResult(
        waiting_confirmation=True, result="Waiting for file edit approval"
    ))
    result = runner.run_once()
    assert result.status == "waiting_confirmation"
    assert store.claim_next(max_attempts=3) is None
    import pytest
    with pytest.raises(ValueError, match="settled first"):
        store.retry(task.id)


def test_cancelled_task_is_not_claimed(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create("Do not run")
    assert store.cancel(task.id).status == "cancelled"
    assert store.claim_next(max_attempts=3) is None


def test_step_limit_fails_without_extra_processor_calls(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create("Bounded work")
    calls = []
    runner = DurableTaskRunner(store, lambda current: calls.append(current) or StepResult(done=False), max_steps=1)
    result = runner.run_once()
    assert result.status == "failed"
    assert result.error == "maximum step limit reached"
    assert len(calls) == 1


def test_create_plan_creates_root_and_subtasks(tmp_path):
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
        # depends_on should be resolved to actual task IDs
        if i == 0:
            assert st.depends_on == []
        else:
            assert st.depends_on == [subtasks[0].id]
        assert st.idempotency_key == f"plan-key-1:step:{i}"


def test_get_plan_tasks_returns_ordered(tmp_path):
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
    store = TaskStore(tmp_path / "tasks.db")
    plan = Plan(root_goal="Test", steps=[
        PlanStep(index=0, goal="A", depends_on=[]),
        PlanStep(index=1, goal="B", depends_on=[0]),
    ], idempotency_key="k")
    root, subs = store.create_plan(plan)
    # Complete step 0: claim -> running -> completed
    store.claim_next(max_attempts=3)
    store.transition(subs[0].id, "completed", result="done")
    # Step 1 should now be claimable (was blocked by deps)
    ready = store.mark_deps_completed(subs[0].id)
    assert len(ready) == 1
    assert ready[0].id == subs[1].id
    # Note: mark_deps_completed returns tasks that became claimable


def test_claim_next_skips_blocked_tasks(tmp_path):
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
    # Complete step 0: claim -> running -> completed
    store.transition(subs[0].id, "completed", result="done")
    # Now step 1 should be claimable
    claimed2 = store.claim_next(max_attempts=3)
    assert claimed2 is not None
    assert claimed2.id == subs[1].id
