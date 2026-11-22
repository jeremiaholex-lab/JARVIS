# Desktop Control and System Facts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add read-only laptop facts and fully confirmed Windows desktop actions with a synchronized cursor overlay.

**Architecture:** Read-only telemetry plugins register beside the existing `system_info` plugin. State-changing desktop tools are builtins that follow the existing propose/confirm/audit pattern, and the orb emits overlay commands to a separate C# cursor window. Every action is gated by typed `yes, do it`, a local screenshot crop, sensitive-window redaction, and a fail-closed shell heartbeat.

**Tech Stack:** Python, FastAPI/WebSocket, pytest, psutil, Win32 `SendInput`/`GetWindowRect`, Windows GDI, C# WinForms/WebView2, JavaScript in `orb/static/index.html`.

**Spec:** `docs/superpowers/specs/2026-11-21-desktop-control-and-system-facts-design.md`

## Global Constraints

- Every state-changing desktop action requires typed `yes, do it`.
- One desktop action per user request in v1.
- Allow documents, folders, common applications, and `.lnk` shortcuts.
- Block `.exe`, `.bat`, `.ps1`, other scripts, and URLs in v1.
- Filter and redact sensitive windows from facts, cards, and targeting.
- Stop controls are HUD Stop and Ctrl+Alt+J; do not intercept Escape globally.
- Confirmation cards include a locally generated screenshot crop.
- Likely password fields trigger a warning but remain allowed after approval.
- Fail closed if the C# shell overlay is unavailable.
- Every proposal, approval, denial, execution, failure, and cancellation is audited.

## Review Focus

- Sensitive window title leaks: a matching password/banking/private window must never appear in facts, cards, crops, or target lists. Test: `test_redacts_sensitive_windows_from_facts_and_targets`.
- Script or URL launch: `.exe`, `.bat`, `.ps1`, scripts, and URLs must be rejected before proposal. Test: `test_unsafe_launch_targets_are_rejected`.
- Stale approval replay: an approval token must not execute twice or after target mutation. Test: `test_stale_or_modified_approval_is_rejected`.
- Wrong cursor target: overlay coordinates must match the approved action, including DPI conversion. Test: `test_cursor_overlay_receives_approved_coordinates`.
- Stop mid-action: HUD stop, Ctrl+Alt+J, or a new turn must prevent all subsequent steps. Test: `test_stop_cancels_pending_action`.

---

### Task 1: Confirmation contract and pending actions

**Files:**
- Create: `jarvis/desktop/__init__.py`
- Create: `jarvis/desktop/actions.py`
- Create: `test_desktop_actions.py`
- Modify: `jarvis/security/tiers.py`
- Modify: `jarvis/agent/registry.py`
- Modify: `jarvis/agent/tools.py`

**Interfaces:**
- Produces: `DesktopAction` dataclass with `action_id: str`, `kind: str`, `target: dict`, `risk: str`, `evidence: dict`, `expires_at: float`; `create_desktop_action_proposal(action: DesktopAction) -> dict`; `resolve_desktop_action(action_id: str, approval: str, snapshot: dict) -> DesktopAction | None`; `cancel_pending_desktop_actions(reason: str) -> None`; `SENSITIVE_PATTERNS`; `redact_sensitive(value: str) -> str`.

- [ ] **Step 1: Write failing tests for proposal immutability and approval**

```python
def test_proposal_does_not_execute_and_is_immutable():
    action = DesktopAction("a1", "click", {"x": 10, "y": 20}, "write", {}, 1.0)
    proposal = create_desktop_action_proposal(action)
    assert proposal["status"] == "pending"
    action.target["x"] = 999
    assert proposal["target"]["x"] == 10

def test_stale_or_modified_approval_is_rejected():
    action = DesktopAction("a1", "click", {"x": 10, "y": 20}, "write", {}, 1.0)
    create_desktop_action_proposal(action)
    assert resolve_desktop_action("a1", "yes, do it", {"x": 999, "y": 20}) is None
    assert resolve_desktop_action("a1", "no", {"x": 10, "y": 20}) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest test_desktop_actions.py -q`
Expected: FAIL on missing `DesktopAction`.

- [ ] **Step 3: Implement redaction, action model, pending store, and tool registration**

Implement typed approval `yes, do it`, expiry, one-shot tokens, snapshot equality, audit events for each transition, READ-tier telemetry allowlist entries, and built-in non-READ desktop tools. Use the existing confirmation and audit patterns in `jarvis/security/confirm.py` and `jarvis/security/file_edit.py`.

- [ ] **Step 4: Add sensitive-window regression test**

```python
def test_redacts_sensitive_windows_from_facts_and_targets():
    assert redact_sensitive("1Password vault") == "[REDACTED]"
    assert redact_sensitive("Chrome Incognito") == "[REDACTED]"
    assert redact_sensitive("Visual Studio Code") == "Visual Studio Code"
```

- [ ] **Step 5: Run tests and full suite**

Run: `pytest test_desktop_actions.py -q && pytest -q`
Expected: PASS, no secret values in output.

- [ ] **Step 6: Commit**

`git add jarvis/desktop/__init__.py jarvis/desktop/actions.py test_desktop_actions.py jarvis/security/tiers.py jarvis/agent/registry.py jarvis/agent/tools.py && git commit -m "feat(desktop): add confirmed action contract" -m "Co-Authored-By: Claude Haiku 5.5 <noreply@anthropic.com>"`

---

### Task 2: Read-only laptop facts

**Files:**
- Create: `jarvis/plugins/system_facts.py`
- Create: `test_system_facts.py`
- Modify: `jarvis/security/tiers.py`
- Modify: `orb/server.py`
- Modify: `orb/static/index.html`

**Interfaces:**
- Consumes: `redact_sensitive()` from Task 1.
- Produces: `battery_facts() -> dict`, `network_facts() -> dict`, `open_app_facts() -> dict`, `laptop_overview() -> dict`; READ tool names `system_battery_status`, `system_network_status`, `system_open_apps`, `system_overview`.

- [ ] **Step 1: Write failing tests**

```python
def test_battery_facts_are_explicit_when_unavailable(monkeypatch):
    monkeypatch.setattr(system_facts.psutil, "sensors_battery", lambda: None)
    assert system_facts.battery_facts()["available"] is False
    assert system_facts.battery_facts()["percent"] is None

def test_open_app_facts_redact_sensitive_titles():
    windows = [{"title": "Bank of Example", "process": "chrome"}]
    facts = system_facts.normalize_windows(windows)
    assert facts[0]["title"] == "[REDACTED]"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest test_system_facts.py -q`
Expected: FAIL on missing module.

- [ ] **Step 3: Implement telemetry tools**

Use psutil and existing `ScreenCapture.list_windows()`. Never expose passwords, saved network keys, or process command lines. Add unknown values as `None`/`available: false`, never guesses.

- [ ] **Step 4: Add orb readback test**

```python
def test_api_agent_exposes_laptop_facts(client, monkeypatch):
    monkeypatch.setattr(system_facts, "laptop_overview", lambda: {"battery": {}, "network": {}, "apps": []})
    payload = client.get("/api/agent").json()
    assert "laptop_facts" in payload
```

- [ ] **Step 5: Run tests and full suite**

Run: `pytest test_system_facts.py orb/test_voice_turns.py -q && pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

`git add jarvis/plugins/system_facts.py test_system_facts.py jarvis/security/tiers.py orb/server.py orb/static/index.html && git commit -m "feat(facts): expose laptop power, network, and app state" -m "Co-Authored-By: Claude Haiku 5.5 <noreply@anthropic.com>"`

---

### Task 3: Win32 desktop boundary

**Files:**
- Create: `jarvis/desktop/win32_control.py`
- Create: `test_win32_control.py`

**Interfaces:**
- Produces: `DesktopControl` protocol with `cursor_pos() -> tuple[int, int]`, `move_cursor(x: int, y: int) -> None`, `click(x: int, y: int, button: str, clicks: int) -> None`, `scroll(x: int, y: int, direction: str, amount: int) -> None`, `type_text(text: str) -> None`, `press_keys(keys: list[str]) -> None`, `focus_window(hwnd: int) -> None`, `window_rect(hwnd: int) -> tuple[int, int, int, int]`.

- [ ] **Step 1: Write fake-boundary tests**

```python
def test_click_uses_approved_coordinates():
    fake = FakeDesktop()
    control = Win32DesktopControl(os=fake)
    control.click(42, 99, "left", 2)
    assert fake.calls[-1] == ("click", 42, 99, "left", 2)

def test_window_rect_is_returned():
    fake = FakeDesktop(window_rect=(1, 2, 300, 400))
    assert Win32DesktopControl(os=fake).window_rect(7) == (1, 2, 300, 400)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest test_win32_control.py -q`
Expected: FAIL on missing class.

- [ ] **Step 3: Implement injected Win32 adapter**

Wrap `GetCursorPos`, `SetCursorPos`, `SendInput`, `GetForegroundWindow`, `SetForegroundWindow`, and `GetWindowRect`. Keep all OS calls behind the injected boundary. Add DPI conversion at this layer only.

- [ ] **Step 4: Run tests**

Run: `pytest test_win32_control.py -q`
Expected: PASS without moving the real cursor.

- [ ] **Step 5: Commit**

`git add jarvis/desktop/win32_control.py test_win32_control.py && git commit -m "feat(desktop): add injectable Win32 input boundary" -m "Co-Authored-By: Claude Haiku 5.5 <noreply@anthropic.com>"`

---

### Task 4: Launch policy and screenshot evidence

**Files:**
- Create: `jarvis/desktop/targets.py`
- Create: `jarvis/desktop/evidence.py`
- Create: `test_desktop_targets.py`

**Interfaces:**
- Consumes: `redact_sensitive()` from Task 1; `DesktopControl` from Task 3.
- Produces: `validate_launch_target(path: str) -> tuple[bool, str]`; `capture_target_crop(control: DesktopControl, x: int, y: int, radius: int = 160) -> bytes`.

- [ ] **Step 1: Write failing target policy tests**

```python
@pytest.mark.parametrize("target", ["app.exe", "setup.bat", "run.ps1", "https://example.com"])
def test_unsafe_launch_targets_are_rejected(target):
    assert validate_launch_target(target)[0] is False

@pytest.mark.parametrize("target", ["Notes.txt", "Projects", "Chrome.lnk"])
def test_safe_launch_targets_are_allowed(target):
    assert validate_launch_target(target)[0] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest test_desktop_targets.py -q`
Expected: FAIL on missing module.

- [ ] **Step 3: Implement validation and local crop capture**

Validate extension and URL schemes before proposal. Generate crops locally with existing screen capture, redact sensitive overlays, and return PNG bytes. Never upload crops without existing vision consent.

- [ ] **Step 4: Run tests**

Run: `pytest test_desktop_targets.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

`git add jarvis/desktop/targets.py jarvis/desktop/evidence.py test_desktop_targets.py && git commit -m "feat(desktop): enforce launch policy and local card evidence" -m "Co-Authored-By: Claude Haiku 5.5 <noreply@anthropic.com>"`

---

### Task 5: Action executor and cancellation

**Files:**
- Create: `jarvis/desktop/executor.py`
- Create: `test_desktop_executor.py`
- Modify: `orb/server.py`

**Interfaces:**
- Consumes: `resolve_desktop_action()` from Task 1, `DesktopControl` from Task 3, `validate_launch_target()` from Task 4.
- Produces: `DesktopActionExecutor(control: DesktopControl)`, `execute(action_id: str, approval: str, snapshot: dict) -> dict`, `cancel(reason: str) -> None`, `is_cancelled() -> bool`.

- [ ] **Step 1: Write failing cancellation test**

```python
def test_stop_cancels_pending_action():
    executor = DesktopActionExecutor(FakeDesktop())
    create_desktop_action_proposal(click_action)
    executor.cancel("hud stop")
    assert executor.execute("a1", "yes, do it", click_snapshot)["status"] == "cancelled"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest test_desktop_executor.py -q`
Expected: FAIL on missing module.

- [ ] **Step 3: Implement executor**

Resolve approval, revalidate snapshot and launch policy, check cancellation, emit action status events, execute exactly one approved action, and audit success/failure/cancellation.

- [ ] **Step 4: Add new-turn cancellation integration test**

```python
def test_new_user_turn_cancels_pending_desktop_action():
    executor = DesktopActionExecutor(FakeDesktop())
    create_desktop_action_proposal(click_action)
    server.cancel_pending_desktop_actions("new turn")
    assert executor.is_cancelled() is True
```

- [ ] **Step 5: Run tests and full suite**

Run: `pytest test_desktop_executor.py orb/test_text_turn_speech.py -q && pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

`git add jarvis/desktop/executor.py test_desktop_executor.py orb/server.py && git commit -m "feat(desktop): add confirmed action executor and cancellation" -m "Co-Authored-By: Claude Haiku 5.5 <noreply@anthropic.com>"`

---

### Task 6: Cursor overlay protocol and C# window

**Files:**
- Create: `orb/CursorOverlayForm.cs`
- Add embedded overlay HTML/CSS/JS resource or file under `orb/static/` chosen by implementer
- Modify: `orb/JARVIS.Orb.csproj`
- Modify: `orb/Program.cs`
- Modify: `orb/OverlayForm.cs`
- Modify: `orb/server.py`
- Modify: `orb/static/index.html`
- Create: `test_desktop_overlay_protocol.py`

**Interfaces:**
- Consumes: `DesktopActionExecutor` events from Task 5.
- Produces: WebSocket ops `desktop_confirm`, `desktop_cursor_show`, `desktop_cursor_move`, `desktop_cursor_state`, `desktop_cursor_hide`, `desktop_hud_action`, `desktop_result`; helper `emit_desktop_action_events(action: DesktopAction, evidence: bytes = b"") -> list[dict]`; helper `prepare_desktop_action(action: DesktopAction, overlay_connected: bool) -> dict`; C# handler method `ApplyDesktopCursorCommand(string json)`.

- [ ] **Step 1: Write failing protocol tests**

```python
def test_cursor_overlay_receives_approved_coordinates():
    events = emit_desktop_action_events(action, evidence=b"png")
    moved = [e for e in events if e["op"] == "desktop_cursor_move"]
    assert moved[0]["x"] == 10 and moved[0]["y"] == 20

def test_overlay_absence_fails_closed():
    assert prepare_desktop_action(action, overlay_connected=False)["status"] == "blocked"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest test_desktop_overlay_protocol.py -q`
Expected: FAIL on missing event helpers.

- [ ] **Step 3: Implement backend event sequence and orb card**

Add confirmation card with screenshot crop, redacted target text, exact text/keys, and typed approval input. Add WebSocket dispatch and an overlay heartbeat with fail-closed behavior. Do not execute without `overlay_connected: true`.

- [ ] **Step 4: Implement C# cursor overlay**

Create a transparent, click-through, always-on-top layer with ring/crosshair, state colors, action label, HUD Stop button, and Ctrl+Alt+J handling through the existing hotkey window. Never steal focus.

- [ ] **Step 5: Build and smoke test desktop shell**

Run: `dotnet build orb/JARVIS.Orb.csproj`
Expected: build succeeds with no new errors. Existing WebView2 version-conflict warnings are acceptable.

- [ ] **Step 6: Run tests and full suite**

Run: `pytest test_desktop_overlay_protocol.py -q && pytest -q`
Expected: PASS.

- [ ] **Step 7: Commit**

`git add orb/CursorOverlayForm.cs orb/JARVIS.Orb.csproj orb/Program.cs orb/OverlayForm.cs orb/server.py orb/static/index.html test_desktop_overlay_protocol.py && git commit -m "feat(orb): add synchronized desktop cursor overlay" -m "Co-Authored-By: Claude Haiku 5.5 <noreply@anthropic.com>"`

---

### Task 7: End-to-end action tools and documentation

**Files:**
- Modify: `jarvis/agent/tools.py`
- Modify: `jarvis/security/tiers.py`
- Modify: `README.md`
- Modify: `DESIGN.md`
- Create: `test_desktop_tool_integration.py`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: callable tools `desktop_open_app`, `desktop_open_file`, `desktop_move_cursor`, `desktop_click`, `desktop_scroll`, `desktop_type`, `desktop_press_keys`.

- [ ] **Step 1: Write failing integration tests**

```python
def test_desktop_click_tool_proposes_only():
    tools = build_tools()
    result = execute("desktop_click", {"x": 10, "y": 20}, context=FakeContext())
    assert result["status"] == "pending_confirmation"
    assert FakeDesktop().calls == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest test_desktop_tool_integration.py -q`
Expected: FAIL on missing tool.

- [ ] **Step 3: Wire tools into registry and executor**

Register all seven as built-in non-READ tools, route through proposal/approval, enforce one action per turn, and reject batching.

- [ ] **Step 4: Run focused and full suites**

Run: `pytest test_desktop_tool_integration.py tests/test_tool_exposure.py tests/test_tool_registry.py -q && pytest -q`
Expected: PASS.

- [ ] **Step 5: Update documentation**

Document capabilities, confirmation rules, stop controls, hidden sensitive windows, safe launch policy, and that password-field entry warns but remains allowed. Include example prompts.

- [ ] **Step 6: Commit**

`git add jarvis/agent/tools.py jarvis/security/tiers.py README.md DESIGN.md test_desktop_tool_integration.py && git commit -m "feat(desktop): expose confirmed desktop control tools" -m "Co-Authored-By: Claude Haiku 5.5 <noreply@anthropic.com>"`

---

## Final Verification

- [ ] Run `pytest -q`; require zero failures.
- [ ] Run `dotnet build orb/JARVIS.Orb.csproj`; require no new errors.
- [ ] Launch JARVIS with `orb/launch_orb.bat`.
- [ ] Ask: "What is my battery status and what apps are open?" Verify read-only answer.
- [ ] Ask JARVIS to click a harmless target. Verify card, screenshot crop, typed approval, cursor states, and audit event.
- [ ] Press Ctrl+Alt+J during a pending action and verify cancellation.
- [ ] Attempt `.exe`, script, and URL launch; verify rejection before any confirmation.
- [ ] Confirm a sensitive window is absent from facts and cards.
- [ ] Review `git status`; working tree clean after final commit.
