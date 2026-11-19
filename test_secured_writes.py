"""Stage 2 write surface: jarvis_projects_write, proposed then host-confirmed.

Offline — a real ProjectStore on tmp_path, a deterministic-only auditor, no
Ollama, no network. The invariant these tests pin: a write is *proposed*,
never executed, until the host resolves the token with approval.
"""
import json

import pytest

from jarvis.agent import tools as agent_tools
from jarvis.security.confirm import SecuredWrites, WRITE_TOOL
from jarvis.security.auditor import Auditor, Decision
from jarvis.security.tiers import Tier, classify, PlannedAction


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_PROJECT_DB", str(tmp_path / "p.db"))
    return agent_tools._project_store()


def make_exec(store, tmp_path):
    """A deterministic-only SecuredWrites with no audit logger (stays offline)."""
    ex = SecuredWrites(store, Auditor(use_llm=False), audit_logger=None)
    return ex


# ------------------------------------------------------------------ tool offer

def test_write_tool_is_offered_and_write_tier():
    available = agent_tools.build_tools()
    tool = next(t for t in available if t.name == WRITE_TOOL)
    assert tool.tier is Tier.WRITE
    schemas = {s["function"]["name"] for s in agent_tools.tool_schemas(available)}
    assert WRITE_TOOL in schemas


def test_classify_says_write_for_projects_write():
    assert classify(PlannedAction(tool=WRITE_TOOL, params={})) is Tier.WRITE


def test_execute_refuses_a_real_mutation_tool():
    # shell_exec is not in the orb tool set at all -> "not available".
    out = json.loads(agent_tools.execute(agent_tools.build_tools(), "shell_exec", "{}"))
    assert "error" in out


def test_propose_is_side_effect_free(store, tmp_path, monkeypatch):
    ex = make_exec(store, tmp_path)
    agent_tools.configure_writes(ex)
    out = json.loads(agent_tools.execute(agent_tools.build_tools(), WRITE_TOOL,
                                         {"action": "add_project", "name": "P", "path": "/tmp/p"}))
    assert out["pending"] is True and out["token"]
    assert store.list_projects() == []  # nothing written yet
    agent_tools.configure_writes(None)


# ------------------------------------------------------------------ executor

def test_propose_then_approve_executes(store, tmp_path):
    ex = make_exec(store, tmp_path)
    r = ex.propose({"action": "add_project", "name": "Alpha", "path": "/tmp/alpha"})
    assert r["pending"] and store.list_projects() == []
    res = ex.resolve(r["token"], True)
    assert not res.get("error")
    assert [p["name"] for p in store.list_projects()] == ["Alpha"]


def test_reject_changes_nothing(store, tmp_path):
    ex = make_exec(store, tmp_path)
    r = ex.propose({"action": "add_project", "name": "Ghost", "path": "/tmp/ghost"})
    ex.resolve(r["token"], False)
    assert store.list_projects() == []
    assert ex.pending() == []


def test_deadline_propose_requires_known_project(store, tmp_path):
    ex = make_exec(store, tmp_path)
    pr = ex.propose({"action": "add_project", "name": "Beta", "path": "/tmp/beta"})
    ex.resolve(pr["token"], True)  # the project only exists once confirmed
    r = ex.propose({"action": "add_deadline", "project_name": "Beta",
                    "title": "ship", "due_date": "2026-12-01"})
    out = ex.resolve(r["token"], True)["outcome"]
    assert out.get("created_deadline_id")
    assert [d["title"] for d in store.list_deadlines()] == ["ship"]


def test_unknown_project_name_is_an_error_not_a_crash(store, tmp_path):
    ex = make_exec(store, tmp_path)
    r = ex.propose({"action": "add_deadline", "project_name": "Nope",
                    "title": "x", "due_date": "2026-12-01"})
    out = ex.resolve(r["token"], True)["outcome"]
    assert "error" in out


def test_bad_action_is_rejected_without_a_token(store, tmp_path):
    ex = make_exec(store, tmp_path)
    out = ex.propose({"action": "drop_table"})
    assert out["pending"] is False and "error" in out
    assert ex.pending() == []


def test_secret_shaped_write_is_held_by_auditor(store, tmp_path):
    ex = make_exec(store, tmp_path)
    r = ex.propose({"action": "add_project", "name": "S", "path": "/creds/.env"})
    # The deterministic critic must HOLD a write that touches a secret path.
    assert r["auditor"] == Decision.HOLD.value


def test_double_resolve_is_refused(store, tmp_path):
    ex = make_exec(store, tmp_path)
    r = ex.propose({"action": "add_project", "name": "Once", "path": "/tmp/once"})
    ex.resolve(r["token"], True)
    second = ex.resolve(r["token"], True)
    assert "error" in second
