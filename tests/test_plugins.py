"""Plugin system tests (roadmap #7): loader, sample plugin, tier allowlist.

Loader failure modes are exercised through a fake on-disk plugin package in
tmp_path (real module files, real import — no monkeypatching of pkgutil).
"""
from __future__ import annotations

import json
import textwrap

import pytest

from jarvis.agent.plugins import load_plugin_tools
from jarvis.agent.registry import ToolRegistry
from jarvis.agent.tools import AgentTool, build_tools
from jarvis.security.tiers import Tier, classify, PlannedAction


# ---------------------------------------------------------------------------
# tier allowlist
# ---------------------------------------------------------------------------

def test_allowlisted_plugin_name_classifies_read():
    assert classify(PlannedAction("system_info")) is Tier.READ


def test_unlisted_extension_name_stays_write():
    # The default-WRITE floor is what keeps an unlisted name un-registerable.
    assert classify(PlannedAction("some_plugin_tool")) is Tier.WRITE


def test_registry_rejects_declared_tier_drift():
    # A plugin that lies about its own tier is refused: the classifier is the
    # source of truth, and system_info classifies READ.
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="security policy classifies"):
        registry.register(AgentTool(
            name="system_info", description="liar",
            parameters={"type": "object", "properties": {}},
            fn=lambda: "x", tier=Tier.WRITE,
        ))


# ---------------------------------------------------------------------------
# sample plugin + end-to-end build
# ---------------------------------------------------------------------------

def test_build_tools_includes_system_info_plugin():
    tools = build_tools(load_plugin_tools())
    names = {t.name for t in tools}
    assert "system_info" in names


def test_system_info_tool_returns_json_snapshot():
    tools = build_tools(load_plugin_tools())
    system_info = next(t for t in tools if t.name == "system_info")
    payload = json.loads(system_info.fn())
    assert "cpu_percent" in payload or "error" in payload


# ---------------------------------------------------------------------------
# loader failure modes via a fake on-disk plugin package
# ---------------------------------------------------------------------------

def _make_fake_package(tmp_path, monkeypatch, name="fake_plugins"):
    """A real importable package: one healthy plugin, one that raises on
    import, one that is not a plugin at all."""
    pkg_dir = tmp_path / name
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "ok.py").write_text(textwrap.dedent("""
        from jarvis.agent.tools import AgentTool
        from jarvis.security.tiers import Tier

        def tools():
            return [AgentTool(
                name="fake_tool", description="ok plugin",
                parameters={"type": "object", "properties": {}},
                fn=lambda: "ok", tier=Tier.READ,
            )]
    """))
    (pkg_dir / "broken.py").write_text("raise ImportError('boom')")
    (pkg_dir / "notaplugin.py").write_text("VALUE = 1")
    monkeypatch.syspath_prepend(str(tmp_path))
    import importlib
    importlib.import_module(name)


def test_loader_collects_tools_from_healthy_plugins(tmp_path, monkeypatch):
    _make_fake_package(tmp_path, monkeypatch)
    found = load_plugin_tools("fake_plugins", builtin_names={"jarvis_projects_read"})
    assert [t.name for t in found] == ["fake_tool"]


def test_loader_skips_broken_and_non_plugin_modules(tmp_path, monkeypatch):
    _make_fake_package(tmp_path, monkeypatch)
    found = load_plugin_tools("fake_plugins", builtin_names={"unrelated"})
    # broken.py raises on import; notaplugin.py has no tools() — only ok.py.
    assert [t.name for t in found] == ["fake_tool"]


def test_loader_rejects_tools_shadowing_builtins(tmp_path, monkeypatch):
    _make_fake_package(tmp_path, monkeypatch)
    found = load_plugin_tools("fake_plugins", builtin_names={"fake_tool"})
    assert [t.name for t in found] == []
