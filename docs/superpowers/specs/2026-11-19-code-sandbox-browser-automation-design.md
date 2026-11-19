# Design Document – Code Sandbox & Browser Automation Integration

**Date:** 2026-11-19

---

## 1. Intent & Motivation

The JARVIS agent needs two new capabilities:

1. **Secure code sandbox** – Execute untrusted Python snippets safely. Existing `openjarvis_runtime/tools/code_interpreter.py` already provides an AST‑validated interpreter, but we require isolation at the process level (Docker container first, fallback to a restricted subprocess) to protect the host system.

2. **Browser automation** – Allow agents to interact with web pages (navigate, click, type, screenshot, extract). The repository already contains a Playwright‑based `openjarvis_runtime/src/openjarvis/tools/browser.py` providing five tools.

These capabilities will be exposed to the agent framework via the Jarvis tool registry and will be usable from both the Orb HTTP API and the Telegram Brain host.

---

## 2. Requirements

| Requirement | Description |
|-------------|-------------|
| **Isolation** | Docker container with `--network none`, memory cap (~4 GB), read‑only rootfs. If Docker is unavailable, use a restricted subprocess with environment sanitisation, network poisoning, and a watchdog that kills the process on memory over‑use.
| **AST Validation** | Reject imports outside the allow‑list (`json`, `math`, `time`) and any dangerous names (`eval`, `exec`, `open`, etc.) before execution.
| **SSRF Protection** | All URLs passed to `browser_navigate` must be validated by `openjarvis.security.ssrf.check_ssrf` to prevent server‑side request forgery.
| **Tool Tiering** | Sandbox execution is a **WRITE**‑tier operation (it may affect host resources). Browser automation is a **READ**‑tier operation because it only fetches external data; however, actions that mutate remote sites (e.g., clicks that submit forms) are still classified as READ for the purpose of the Orb tier model.
| **Telemetry** | Each tool must emit a `ToolResult` with `metadata` describing execution environment (`isolation`, `docker_used`, `browser_url`, etc.) for observability and debugging.
| **Testing** | Existing test suites (`openjarvis_runtime/tests/tools/test_code_interpreter.py`, `jarvis/skills/sandbox_test.py`) must be extended with integration tests for the new public tools.

---

## 3. Constraints

* Python 3.11+ only; no new third‑party dependencies beyond Playwright (already optional) and Docker CLI.
* All task results must be JSON‑serialisable (fits existing Orb `/api/tasks` contract).
* Max sub‑tasks per plan: 5 (as enforced by the planner). The sandbox and browser tools will be used as sub‑tasks within a plan.
* Execution time limits: 30 s for sandboxed code, 60 s for browser navigation.

---

## 4. Architecture Overview

### 4.1 Sandbox Execution Flow

```
User → Orb API (/api/tasks) → TaskRunner → Planner →
    Sub‑task: run_skill_tests(test_script)
        ├─ Prefer Docker? → docker_cli() → _run_docker()
        └─ Fallback → _run_restricted() with psutil watchdog
    → Result stored in TaskStore (plan_id, step_index)
```

* `run_skill_tests` (in `jarvis/skills/sandbox_test.py`) already implements the Docker‑first strategy and the fallback process.
* We will expose a new Jarvis tool `sandbox_execute` that wraps `run_skill_tests` and returns a concise `ToolResult`.

### 4.2 Browser Automation Flow

```
User → Jarvis → ToolRegistry (browser_* tools) → _BrowserSession
    ├─ browser_navigate(url) → SSRF check → page.goto()
    ├─ browser_click(selector) → strategy ladder → element.click()
    ├─ browser_type(selector, text) → page.fill()/type()
    ├─ browser_screenshot() → page.screenshot() → base64
    └─ browser_extract() → page.inner_text / eval_on_selector_all()
```

* A singleton `_BrowserSession` lazily launches Playwright's Chromium instance.
* All tools share the same session, ensuring stateful navigation across multiple sub‑tasks.
* The session is closed automatically when the process exits; we could add a `browser_close` tool in the future.

---

## 5. Integration Points

| Component | Change Needed |
|-----------|----------------|
| `jarvis/agent/tools.py` | Register two new tool classes: `SandboxExecuteTool` (WRITE tier) and `BrowserNavigateTool`/`BrowserClickTool`/`BrowserTypeTool`/`BrowserScreenshotTool`/`BrowserExtractTool` (already registered, but we will expose them through the Orb API by adding them to the tier‑filter list).
| `orb/server.py` | Extend `/api/tools` endpoint to include the new tool IDs, ensuring the tier‑metadata (`tier: "write"` for sandbox, `tier: "read"` for browser).
| `jarvis/agent/task_runner.py` | No change required – tasks already store `tool_name` and `metadata`. Ensure sandbox results include `isolation` field.
| `test_task_runner.py` | Add tests for the new `sandbox_execute` tool and a basic navigation test using a local HTTP server (e.g., `http.server` on localhost).
| Documentation (`docs/`) | Add this design doc and update the SDD ledger (`docs/superpowers/ledger.md`).

---

## 6. Implementation Plan (to be fed to `superpowers:writing-plans`)

1. Add `SandboxExecuteTool` class in `jarvis/skills/sandbox_tool.py` that calls `run_skill_tests`.
2. Update `jarvis/agent/tools.py` to import and register the sandbox tool.
3. Verify Docker detection on Windows (use `_win_executable` helper already in `sandbox_test.py`).
4. Add simple health‑check endpoint `/api/tools` to list available tools with tier info.
5. Write integration tests:
   * `test_sandbox_execute_success` – run a harmless script.
   * `test_sandbox_deny_unsafe` – ensure blocked import raises error.
   * `test_browser_navigate_local` – start a temporary HTTP server, navigate, verify title.
6. Update CI config to install Playwright browsers (`playwright install`).
7. Documentation: add usage examples to `docs/superpowers/README.md`.

---

## 7. Open Questions (already clarified)
* **Docker fallback** – Use fallback when Docker CLI is missing or engine not running. Already handled by `run_skill_tests`.
* **Browser tier** – Classified as READ due to data‑fetch nature; no mutation of host state.

---

## 8. Risks & Mitigations
* **Docker engine unavailable** – Fallback process is less isolated; we clearly surface `isolation="fallback"` in the tool result.
* **Playwright installation** – Require optional extra `browser`. The tool will return a helpful error message if missing.
* **Memory watchdog false positives** – Marker file `memory-kill` is created only when the limit is exceeded; we log the event for debugging.

---

*Design document prepared by Claude Sonnet 5.*
