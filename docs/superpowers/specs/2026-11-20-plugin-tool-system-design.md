# Design: Plugin/skill tool system (roadmap item #7)

Date: 2026-11-20
Status: implemented and verified (tests green, 154 passed / 3 skipped)

## Why

Roadmap item #7. Every new capability today requires touching the core
`build_tools()` catalog — no extension surface. A plugin module should be able
to add a read-only tool that both hosts (the voice orb, the text brain /
Telegram) pick up at startup, without either host knowing the plugin exists.

## Goals

- Hosts call `build_tools(load_plugin_tools())`; one line each, no other
  wiring.
- Plugin module = one file in `jarvis/plugins/` exposing `tools() -> List[AgentTool]`.
  The sample `system_info` (psutil CPU/memory/disk snapshot) is the reference.
- One broken plugin never breaks host startup: import failure, a raising
  `tools()`, wrong item type, or a name collision each skips that plugin with
  a log warning.
- A plugin can only ever register READ-tier tools.

## Non-goals (v1)

- Out-of-repo plugin packages (discovery is host-driven, inside
  `jarvis/plugins/` — no path scanning, no auto-import of third-party code).
- Write-capable plugins, plugin lifecycle hooks, plugin settings.

## Security model: closed for writes, open for reads

`classify()` in `jarvis.security.tiers` defaults any unknown tool name to
WRITE. That default is the gate: an extension tool is registerable only while
its name is explicitly allowlisted in tiers.py as READ. The registry
(`jarvis/agent/registry.py`) cross-checks each entry's declared tier against
the classifier, so a plugin that mis-declares its tier is rejected at build
time. Adding a plugin name is therefore always a repo code change to
`_EXTENSION_TOOL_NAMES` — a closed write surface, an open read surface.

## Approaches

**A. In-repo package scan + tier allowlist (chosen).** `pkgutil` scan of
`jarvis/plugins/`, fail-soft per module, names allowlisted in tiers.py. No new
process or trust boundary; the security rule is enforced by the same classifier
that guards built-ins.

**B. Manifest/config-file plugin registry.** Configurable but re-opens the
surface: a config file becomes a write vector (someone edits the JSON, a tool
appears) with no code-review trail. The allowlist-PR model keeps every
capability addition visible in git.

**C. Subprocess isolation per plugin.** Stronger blast-radius, but a second
host to babysit for tools that are read-only local probes; disproportionate
until write-capable plugins exist.

## What landed

- `jarvis/agent/registry.py` — `ToolRegistry`: name/schema validation,
  duplicate rejection, declared-tier vs classifier cross-check, atomic
  `register_many` (a bad entry fails the whole batch, never pollutes).
- `jarvis/agent/plugins.py` — `load_plugin_tools()`: fail-soft discovery,
  skips `_`-prefixed modules, shadows builtins are dropped.
- `jarvis/security/tiers.py` — `_EXTENSION_TOOL_NAMES` allowlist checked in
  `classify()` before the reads-free step.
- `jarvis/plugins/system_info.py` — reference plugin; `jarvis/plugins/` is
  docstring-only on purpose.
- `jarvis/agent/brain.py` + `orb/server.py` — one-line wiring each:
  `build_tools(load_plugin_tools())`.
- `tests/test_plugins.py` (8 tests incl. a fake on-disk package exercising
  every failure mode) + `tests/test_tool_registry.py` (6 tests).
