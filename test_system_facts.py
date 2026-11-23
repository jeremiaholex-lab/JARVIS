"""Read-only laptop facts: explicit unavailability and safe window metadata.

These tests pin the privacy boundary first: sensitive titles never reach a
facts payload, and no telemetry call may guess a value it cannot read.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jarvis.agent.tools import AgentTool
from jarvis.plugins import system_facts
from jarvis.security.tiers import PlannedAction, Tier, classify


@pytest.fixture
def client(monkeypatch):
    """Loopback TestClient with the model/agent seams stubbed like orb tests."""
    import server

    class AgentState:
        tools = []
        cloud_available = False
        local_tok_s = None
        user_model = type("Model", (), {
            "stats": lambda self: {}, "active_facts": lambda self, n: []
        })()
        memory = type("Memory", (), {"stats": lambda self: {}})()

    monkeypatch.setattr(server, "agent_context", lambda: AgentState())
    app = server.build_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as c:
        yield c


def test_battery_facts_are_explicit_when_unavailable(monkeypatch):
    monkeypatch.setattr(system_facts.psutil, "sensors_battery", lambda: None)
    assert system_facts.battery_facts()["available"] is False
    assert system_facts.battery_facts()["percent"] is None


def test_open_app_facts_redact_sensitive_titles():
    windows = [{"title": "Bank of Example", "process": "chrome"}]
    facts = system_facts.normalize_windows(windows)
    assert facts[0]["title"] == "[REDACTED]"


def test_api_agent_exposes_laptop_facts(client, monkeypatch):
    monkeypatch.setattr(system_facts, "laptop_overview",
                        lambda: {"battery": {}, "network": {}, "apps": []})
    payload = client.get("/api/agent").json()
    assert "laptop_facts" in payload
    assert payload["laptop_facts"] == {"battery": {}, "network": {}, "apps": []}


def test_normalize_windows_redacts_process_and_drops_sensitive_fields():
    window = type("Window", (), {
        "title": "1Password vault", "process": "keepass",
        "pid": 42, "hwnd": "secret-handle", "class_name": "Chrome_Widget",
    })()
    facts = system_facts.normalize_windows([window])
    assert facts == [{"title": "[REDACTED]", "process": "[REDACTED]",
                      "pid": 42, "rect": None}]
    assert "hwnd" not in facts[0] and "class_name" not in facts[0]


def test_network_facts_are_explicit_when_probe_fails(monkeypatch):
    def boom():
        raise OSError("probe blocked")

    monkeypatch.setattr(system_facts.psutil, "net_if_stats", boom)
    facts = system_facts.network_facts()
    assert facts["available"] is False
    assert facts["interfaces"] == []


def test_network_facts_never_include_keys_or_command_lines(monkeypatch):
    from types import SimpleNamespace

    class Address:
        family = SimpleNamespace(name="AF_INET")
        address = "192.0.2.10"
        netmask = "255.255.255.0"
        broadcast = None

    monkeypatch.setattr(system_facts.psutil, "net_if_stats",
                        lambda: {"Wi-Fi": SimpleNamespace(isup=True)})
    monkeypatch.setattr(system_facts.psutil, "net_if_addrs",
                        lambda: {"Wi-Fi": [Address()]})
    payload = system_facts.network_facts()
    text = repr(payload)
    assert payload["interfaces"][0]["addresses"] == ["192.0.2.10"]
    for forbidden in ("netmask", "broadcast", "password", "key", "cmdline"):
        assert forbidden not in text.casefold()


@pytest.mark.parametrize("name", (
    "system_battery_status", "system_network_status",
    "system_open_apps", "system_overview",
))
def test_telemetry_tools_are_registerable_read_tools(name):
    tool = next(t for t in system_facts.tools() if t.name == name)
    assert isinstance(tool, AgentTool)
    assert tool.tier is Tier.READ
    assert classify(PlannedAction(tool=name)) is Tier.READ
