from __future__ import annotations
import sys
from pathlib import Path
from types import SimpleNamespace
from fastapi.testclient import TestClient
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server

def test_websocket_disconnect_is_a_normal_close() -> None:
    was_ready = server.MODELS.ready
    server.MODELS.ready = True
    try:
        app = server.build_app()
        with TestClient(app, client=("127.0.0.1", 12345)) as client:
            with client.websocket_connect(f"/ws?token={client.app.state.task_api_token}") as websocket:
                hello = websocket.receive_json()
                assert hello["op"] == "hello"
            # Exiting websocket_connect sends a normal close. The app must
            # finish the session without surfacing WebSocketDisconnect.
    finally:
        server.MODELS.ready = was_ready

def test_shutdown_route_requests_uvicorn_graceful_exit(monkeypatch) -> None:
    was_ready = server.MODELS.ready
    server.MODELS.ready = True
    monkeypatch.setenv("JARVIS_ORB_MANAGED", "1")
    monkeypatch.setenv("OPENJARVIS_HOME", str(server.JARVIS / ".openjarvis"))
    try:
        app = server.build_app()
        uvicorn_server = SimpleNamespace(should_exit=False)
        app.state.uvicorn_server = uvicorn_server
        with TestClient(app, client=("127.0.0.1", 12345)) as client:
            response = client.post("/api/shutdown")
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert uvicorn_server.should_exit is True
    finally:
        server.MODELS.ready = was_ready
