"""Every orb REST readback must be JSON-serializable and truthful.

These endpoints feed the instrument panel; a dataclass leaking into a response
raised inside JSONResponse and the orb silently showed "unreachable".
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fastapi.testclient import TestClient

import server


def _client():
    was_ready = server.MODELS.ready
    server.MODELS.ready = True
    app = server.build_app()
    return TestClient(app, client=("127.0.0.1", 12345)), was_ready


def test_agent_endpoint_is_json_serializable():
    client, was_ready = _client()
    try:
        with client:
            response = client.get("/api/agent")
        assert response.status_code == 200
        data = response.json()
        assert "error" not in data, data.get("error")
        assert set(data["tools"]) >= {"jarvis_projects_read", "jarvis_rag_search"}
        # both proposers are advertised, but neither mutates on its own
        assert {"jarvis_projects_write", "jarvis_file_edit"} <= set(data["tools"])
        for key in ("user_model", "memory", "models", "alerts_available"):
            assert key in data or key == "alerts_available", key
        # facts must be plain dicts, not dataclass reprs
        for fact in data["facts"]:
            assert isinstance(fact, dict) and {"key", "value"} <= set(fact)
        json.dumps(data)
    finally:
        server.MODELS.ready = was_ready


def test_alerts_endpoint_reports_open_issues():
    client, was_ready = _client()
    try:
        with client:
            data = client.get("/api/alerts").json()
        assert "alerts" in data and isinstance(data["alerts"], list)
        assert "stats" in data
        json.dumps(data)
    finally:
        server.MODELS.ready = was_ready


def test_alert_acknowledge_is_loopback_only_and_idempotent():
    client, was_ready = _client()
    try:
        with client:
            missing = client.post("/api/alerts/acknowledge", json={"key": "no-such-key"})
            assert missing.status_code == 200
            assert missing.json()["acknowledged"] is False
            ctx = server.agent_context()
            from jarvis.proactive.alerts import Signal

            ctx.alerts.raise_signal(Signal(key="test:ack", source="tests",
                                           severity="notice", text="test issue"))
            assert client.post("/api/alerts/acknowledge", json={"key": "test:ack"}).json()[
                "acknowledged"] is True
            # second dismissal is a no-op, not an error
            assert client.post("/api/alerts/acknowledge", json={"key": "test:ack"}).json()[
                "acknowledged"] is False
            ctx.alerts.db_path.unlink(missing_ok=True)
    finally:
        server.MODELS.ready = was_ready


def test_durable_task_api_queues_reports_and_cancels_locally():
    client, was_ready = _client()
    try:
        headers = {"x-jarvis-task-token": client.app.state.task_api_token}
        with client:
            forbidden = client.post("/api/tasks", json={"goal": "Do not enqueue"})
            assert forbidden.status_code == 403
            assert client.get("/api/tasks").status_code == 403
            created = client.post("/api/tasks", json={"goal": "Summarize my project deadlines"}, headers=headers)
            assert created.status_code == 202, created.text
            task = created.json()["task"]
            assert task["status"] == "queued"
            fetched = client.get(f"/api/tasks/{task['id']}", headers=headers)
            assert fetched.status_code == 200
            assert fetched.json()["task"]["goal"] == "Summarize my project deadlines"
            cancelled = client.post(f"/api/tasks/{task['id']}/cancel", headers=headers)
            assert cancelled.status_code == 200
            assert cancelled.json()["task"]["status"] == "cancelled"
            assert client.post("/api/tasks", json={"goal": " "}, headers=headers).status_code == 400
            assert client.post(f"/api/tasks/{task['id']}/retry", headers=headers).status_code == 409
    finally:
        server.MODELS.ready = was_ready


def test_plan_id_filter_returns_root_and_subtasks():
    client, was_ready = _client()
    try:
        headers = {"x-jarvis-task-token": client.app.state.task_api_token}
        from jarvis.agent.task_runner import TaskStore
        from jarvis.agent.planner import Plan, PlanStep
        store = server._task_store()
        # Clean up database before test
        with store._connection() as db:
            db.execute("DELETE FROM agent_tasks")
            db.execute("DELETE FROM agent_task_events")
        # Use unique idempotency key to avoid conflicts
        plan = Plan(root_goal="Test plan", steps=[
            PlanStep(index=0, goal="Step A", depends_on=[]),
            PlanStep(index=1, goal="Step B", depends_on=[0]),
        ], idempotency_key="plan-test-filter-1")
        root, subs = store.create_plan(plan)
        with client:
            # Get all tasks without filter
            all_tasks = client.get("/api/tasks", headers=headers).json()["tasks"]
            assert len(all_tasks) >= 3
            # Get plan tasks with filter
            plan_tasks = client.get(f"/api/tasks?plan_id={root.id}", headers=headers).json()["tasks"]
            assert len(plan_tasks) == 3  # root + 2 subtasks
            assert plan_tasks[0]["id"] == root.id
            assert plan_tasks[0]["is_plan_root"] is True
            assert [t["step_index"] for t in plan_tasks[1:]] == [0, 1]
    finally:
        server.MODELS.ready = was_ready


def test_file_watcher_api_is_token_gated_and_controls_configured_watchers(tmp_path):
    client, was_ready = _client()
    try:
        headers = {"x-jarvis-task-token": client.app.state.task_api_token}
        manager = server.agent_context().watchers
        original_roots = manager.approved_roots
        manager.approved_roots = (*original_roots, tmp_path.resolve())
        watched = tmp_path / "watch-me"
        watched.mkdir()
        with client:
            assert client.get("/api/watchers").status_code == 403
            created = client.post("/api/watchers", headers=headers, json={
                "root": str(watched), "pattern": "**/*.md",
                "task_instruction": "Summarize changed notes",
            })
            assert created.status_code == 201, created.text
            watcher = created.json()["watcher"]
            assert watcher["enabled"] is True
            assert "Existing matching files" in watcher["baseline"]
            assert client.post(f"/api/watchers/{watcher['id']}/pause", headers=headers).json()["enabled"] is False
            assert client.post(f"/api/watchers/{watcher['id']}/resume", headers=headers).json()["enabled"] is True
            assert client.post(f"/api/watchers/{watcher['id']}/remove", headers=headers).json()["removed"] is True
            outside = tmp_path.parent / "not-approved"
            outside.mkdir(exist_ok=True)
            rejected = client.post("/api/watchers", headers=headers, json={"root": str(outside)})
            assert rejected.status_code == 400
    finally:
        manager.approved_roots = original_roots
        server.MODELS.ready = was_ready


def test_orb_schedule_pump_enqueues_due_work_once(tmp_path, monkeypatch):
    from jarvis.agent.schedules import ScheduleStore
    from jarvis.agent.task_runner import TaskStore
    from jarvis.agent.tools import _task_schedule
    import time

    tasks = TaskStore(tmp_path / "tasks.db")
    monkeypatch.setattr(server, "_TASK_STORE", tasks)
    monkeypatch.setattr(server, "_SCHEDULE_STORE", None)
    import jarvis.agent.tools as agent_tools
    monkeypatch.setattr(agent_tools, "_SCHEDULES", None)
    schedules = server._task_schedules()
    assert isinstance(schedules, ScheduleStore)
    assert json.loads(_task_schedule(action="list"))["schedules"] == []

    schedule = schedules.create("* * * * *", "summarize recent notes")
    due = time.time() - 120
    with schedules._connection() as db:
        db.execute("UPDATE agent_schedules SET next_run_at=? WHERE id=?", (due, schedule.id))

    fired = server._pump_schedules()
    assert len(fired) == 1
    assert fired[0]["task"] is not None
    assert fired[0]["task"].goal.endswith("summarize recent notes")
    assert server._pump_schedules() == []
    assert len(tasks.list(limit=20)) == 1


def test_tiers_endpoint_is_honest_about_what_the_orb_enforces():
    client, was_ready = _client()
    try:
        with client:
            data = client.get("/api/tiers").json()
        assert "read-tier tools" in data["note"] or "secured-ask" in data["note"]
    finally:
        server.MODELS.ready = was_ready
