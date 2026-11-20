"""Memory-pressure broadcasts reach live websocket clients on transitions.

The durable task loop emits a `pressure` message exactly when the governor
changes state — never on every 2 s tick. Only the loop's side effects are
stubbed (the real task queue and watchers must not run inside a test);
the broadcast logic itself is the shipping code."""
from __future__ import annotations

import itertools
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server

_CYCLE = ("warn", "critical", "ok", "warn", "critical", "ok",
          "warn", "critical", "ok")


class _PressureView:
    def __init__(self, state):
        self.state = state
        self.available_gb = 1.0

    def as_dict(self):
        return {"state": self.state, "available_gb": self.available_gb}


class ScriptedGovernor:
    def __init__(self):
        self.states = iter(_CYCLE)
        self.current = "ok"
        self.calls = 0

    def pressure(self):
        self.calls += 1
        self.current = next(self.states, self.current)
        return _PressureView(self.current)

    def allow_background_work(self):
        return True

    def status(self):
        return {"state": self.current, "available_gb": 1.0}


@pytest.fixture
def pressure_env(monkeypatch):
    was_ready = server.MODELS.ready
    was_gov = server._GOVERNOR
    server.MODELS.ready = True
    server._GOVERNOR = ScriptedGovernor()
    # Stub the loop's side effects; keep its broadcast logic real.
    class _Runner:
        def run_once(self):
            time.sleep(0.05)
            return None
    class _Watchers:
        def poll_once(self):
            return None
    monkeypatch.setattr(server, "_task_runner", lambda: _Runner())
    monkeypatch.setattr(server, "_pump_schedules", lambda: None)
    monkeypatch.setattr(server, "_task_schedules", lambda: None)
    fake_ctx = type("Ctx", (), {"watchers": _Watchers(), "alerts": type(
        "A", (), {"open_alerts": lambda self, n: []})()})()
    monkeypatch.setattr(server, "agent_context", lambda: fake_ctx)
    app = server.build_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as c:
        yield c
    server.MODELS.ready = was_ready
    server._GOVERNOR = was_gov


def test_pressure_transitions_are_broadcast_and_never_spammed(pressure_env):
    c = pressure_env
    with c.websocket_connect("/ws") as ws:
        # drain until the first pressure message
        deadline = time.time() + 40
        seen = []
        while time.time() < deadline:
            m = ws.receive_json()
            if isinstance(m, dict) and m.get("op") == "pressure":
                seen.append(m["state"])
                if len(seen) >= 3:
                    break
        assert len(seen) >= 3, f"only saw {seen}"
        # the loop ticks every 2 s but emits only on CHANGE: no two
        # consecutive broadcasts may repeat a state
        assert all(a != b for a, b in zip(seen, seen[1:])), seen
        assert set(seen) <= {"warn", "critical", "ok"}


def test_status_endpoint_carries_pressure(pressure_env):
    body = pressure_env.get("/api/status").json()
    assert "pressure" in body
    assert body["pressure"]["state"] in ("ok", "warn", "critical")
    assert body["pressure"]["available_gb"] == 1.0
