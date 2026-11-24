"""Loopback surfaces a web page can reach must not hand a browser the agent.

`/ws` is only reachable from the page this server rendered (same-origin,
loopback Host, per-session token); `/static` may serve files under
`orb/static` and nothing else.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server


def _client(*, base_url: str = "http://127.0.0.1:8010", host: str = "127.0.0.1"):
    return TestClient(server.build_app(), base_url=base_url, client=(host, 12345))


def test_static_route_serves_only_files_under_static():
    with _client() as c:
        assert c.get("/static/index.html").status_code == 200
        # drive-absolute and traversal paths resolve outside the static root
        assert c.get("/static/C:/Windows/win.ini").status_code == 404
        assert c.get("/static/%2e%2e/server.py").status_code == 404


def test_ws_rejects_a_rebound_browser_host():
    """DNS rebinding keeps the attacker's hostname in Host and Origin."""
    c = _client(base_url="http://evil.example:8010")
    token = c.app.state.task_api_token
    headers = {"host": "evil.example:8010", "origin": "http://evil.example:8010"}
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(f"/ws?token={token}", headers=headers):
            pass


def test_ws_rejects_a_browser_without_the_task_token():
    c = _client()
    headers = {"host": "127.0.0.1:8010", "origin": "http://127.0.0.1:8010"}
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws", headers=headers):
            pass


def test_ws_rejects_a_browser_with_the_wrong_token():
    c = _client()
    headers = {"host": "127.0.0.1:8010", "origin": "http://127.0.0.1:8010"}
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws?token=not-the-token", headers=headers):
            pass


def test_ws_rejects_a_non_loopback_client():
    c = _client(host="10.0.0.5")
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(f"/ws?token={c.app.state.task_api_token}"):
            pass


def test_ws_accepts_the_served_page():
    c = _client()
    headers = {"host": "127.0.0.1:8010", "origin": "http://127.0.0.1:8010"}
    with c.websocket_connect(f"/ws?token={c.app.state.task_api_token}",
                             headers=headers) as ws:
        ws.send_json({"op": "overlay_connected", "source": "orb"})
