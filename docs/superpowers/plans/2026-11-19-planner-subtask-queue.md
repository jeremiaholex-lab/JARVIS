# Code Sandbox & Browser Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate a secure code sandbox (Docker/fallback) and browser automation tools into JARVIS.

**Architecture:** 
- Expose `run_skill_tests` from `jarvis/skills/sandbox_test.py` as `sandbox_execute` in `jarvis/agent/tools.py`. 
- Expose existing browser tools (`browser_navigate`, `browser_click`, `browser_type`, `browser_screenshot`, `browser_extract`) in `jarvis/agent/tools.py`.
- Update Orb API `/api/tools` to return these tools with tier metadata.

**Tech Stack:** Python 3.11+, Playwright, Docker CLI.

**Spec:** [docs/superpowers/specs/2026-11-19-code-sandbox-browser-automation-design.md](docs/superpowers/specs/2026-11-19-code-sandbox-browser-automation-design.md)

## Global Constraints

- Python 3.11+; no new dependencies; all results JSON-serializable.
- Max sub-tasks per plan: 5; max steps per sub-task: 4; max attempts per sub-task: 2.
- Max wall time per plan: 600s; max total steps per plan: 20.
- Tier: READ (browser), WRITE (sandbox).
- Must work in both Telegram (Brain) and Orb (AgentContext) hosts.

## Review Focus

1. **Sandbox failure under high load:** Verify the memory watchdog kills the process correctly.
2. **SSRF Attempt:** Verify `browser_navigate` blocks `file:///etc/passwd` or internal IP addresses.
3. **Docker Engine absent:** Verify the fallback (restricted process) strategy runs successfully when Docker is missing.
4. **Tool ID collisions:** Ensure browser tool IDs do not conflict with existing registered tools.
5. **Tool Tiering:** Verify that only tools marked `WRITE` tier can mutate host resources.

---

### Task 1: Sandbox Tool Registration

**Files:**
- Create: `jarvis/skills/sandbox_tool.py`
- Modify: `jarvis/agent/tools.py`
- Test: `tests/tools/test_sandbox_tool.py`

**Interfaces:**
- Consumes: `jarvis.skills.sandbox_test.run_skill_tests`
- Produces: `jarvis.agent.tools.SandboxExecuteTool`

- [ ] **Step 1: Write `tests/tools/test_sandbox_tool.py`**
- [ ] **Step 2: Run test to verify it fails (missing tool)**
- [ ] **Step 3: Implement `SandboxExecuteTool` in `jarvis/skills/sandbox_tool.py`**
- [ ] **Step 4: Register in `jarvis/agent/tools.py`**
- [ ] **Step 5: Run tests to verify pass**
- [ ] **Step 6: Commit**

### Task 2: Browser Tool Integration

**Files:**
- Modify: `jarvis/agent/tools.py`
- Test: `tests/tools/test_browser_tools.py`

**Interfaces:**
- Consumes: `openjarvis.tools.browser`
- Produces: `BrowserNavigateTool`, `BrowserClickTool`, `BrowserTypeTool`, `BrowserScreenshotTool`, `BrowserExtractTool` (added to Registry)

- [ ] **Step 1: Write `tests/tools/test_browser_tools.py`**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Register tools in `jarvis/agent/tools.py`**
- [ ] **Step 4: Run tests to verify pass**
- [ ] **Step 5: Commit**

### Task 3: API & Tier Exposure

**Files:**
- Modify: `orb/server.py`
- Test: `orb/test_endpoints.py`

**Interfaces:**
- Consumes: `jarvis.agent.tools.Registry`
- Produces: Updated `/api/tools` response

- [ ] **Step 1: Write `orb/test_endpoints.py` (test new tool IDs in response)**
- [ ] **Step 2: Modify `orb/server.py` to expose tools with tier metadata**
- [ ] **Step 3: Run tests to verify pass**
- [ ] **Step 4: Commit**

### Task 4: Documentation & CI

**Files:**
- Modify: `docs/superpowers/README.md`, `docs/superpowers/ledger.md`, `ci.yml`

- [ ] **Step 1: Add documentation to `docs/superpowers/README.md`**
- [ ] **Step 2: Update `docs/superpowers/ledger.md`**
- [ ] **Step 3: Add `playwright install` to CI configuration**
- [ ] **Step 4: Commit**
