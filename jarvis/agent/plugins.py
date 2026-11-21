"""Plugin discovery: collect the AgentTools that jarvis/plugins exposes.

Hosts call :func:`load_plugin_tools` at startup and hand the result to
``build_tools(extension_tools=...)``. Trust model: the plugin package is
version-controlled code in this repo, so discovery is plain — scan the
package for modules that expose ``tools()``. What is *not* trusted is a
module's correctness: every failure below (bad import, missing/raising
``tools()``, wrong item type, a name shadowing a builtin, duplicate
names) skips that plugin with a warning and keeps the host running. The
registry (jarvis/agent/registry.py) still enforces the hard rule — an
extension tool must classify READ — when the host builds its catalog.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Iterable, List, Optional

from jarvis.agent.tools import AgentTool

_logger = logging.getLogger(__name__)

PLUGIN_PACKAGE = "jarvis.plugins"


def load_plugin_tools(
    package: str = PLUGIN_PACKAGE,
    builtin_names: Optional[Iterable[str]] = None,
) -> List[AgentTool]:
    """Import every plugin module in ``package`` and gather its tools.

    ``builtin_names`` (default: the names in ``build_tools()``) are treated
    as reserved: a plugin tool shadowing one is skipped, not registered.
    """
    from jarvis.agent import tools as agent_tools

    if builtin_names is None:
        builtin_names = {t.name for t in agent_tools.build_tools()}
    else:
        builtin_names = set(builtin_names)

    pkg = importlib.import_module(package)
    found: List[AgentTool] = []
    seen: set[str] = set()
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        if mod_info.name.startswith("_"):
            continue
        module = _import_plugin_module(f"{pkg.__name__}.{mod_info.name}")
        if module is None:
            continue
        plugin_tools = _plugin_tools(module)
        if plugin_tools is None:
            continue
        for tool in plugin_tools:
            if not isinstance(tool, AgentTool):
                _logger.warning("plugin %s returned a non-AgentTool entry; skipped",
                                mod_info.name)
                continue
            if tool.name in builtin_names:
                _logger.warning("plugin tool %r shadows a builtin name; skipped", tool.name)
                continue
            if tool.name in seen:
                _logger.warning("plugin tool %r registered by an earlier plugin; "
                                "kept the first", tool.name)
                continue
            seen.add(tool.name)
            found.append(tool)
    return found


def _import_plugin_module(dotted_name: str):
    try:
        return importlib.import_module(dotted_name)
    except Exception as exc:
        _logger.warning("plugin %s failed to import (%s: %s)",
                        dotted_name.rsplit(".", 1)[-1], exc.__class__.__name__, exc)
        return None


def _plugin_tools(module) -> Optional[List[AgentTool]]:
    """The module's ``tools()`` list, or None if the module has no plugin."""
    factory = getattr(module, "tools", None)
    if factory is None:
        return None  # not a plugin module
    try:
        items = factory()
    except Exception as exc:
        _logger.warning("plugin %s tools() raised (%s: %s); skipped",
                        module.__name__.rsplit(".", 1)[-1], exc.__class__.__name__, exc)
        return None
    if not isinstance(items, (list, tuple)):
        _logger.warning("plugin %s tools() returned %s, expected a list; skipped",
                        module.__name__.rsplit(".", 1)[-1], type(items).__name__)
        return None
    return list(items)


__all__ = ["load_plugin_tools", "PLUGIN_PACKAGE"]
