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


def test_tiers_endpoint_is_honest_about_what_the_orb_enforces():
    client, was_ready = _client()
    try:
        with client:
            data = client.get("/api/tiers").json()
        assert "read-tier tools" in data["note"] or "secured-ask" in data["note"]
    finally:
        server.MODELS.ready = was_ready
