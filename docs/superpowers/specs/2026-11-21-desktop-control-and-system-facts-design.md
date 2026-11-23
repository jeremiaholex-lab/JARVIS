# Desktop Control and Live System Facts Design

Date: 2026-11-21
Status: Final draft for approval

## Intent

Extend JARVIS from reading the screen to safely operating the Windows desktop. The user wants JARVIS to open apps and files, click and type through a special cursor synchronized with its actions, and answer current questions about the laptop: battery, charging state, network status, and open apps.

The defining rule is explicit operator control: **every state-changing desktop action requires a confirmation card**. No click, keystroke, launch, or file open may happen just because the model decided it would be useful.

## Goals

- Add read-only system fact tools for battery, charging state, power estimate, network interfaces/services, and open windows/processes.
- Add permissioned tools to open applications and files, move/click the OS pointer, scroll, type text, and press keys.
- Add a transparent desktop cursor and compact action HUD that reflects queued, acting, done, and stopped states.
- Stream confirmation cards and action status to the orb through existing WebSocket conventions.
- Keep all desktop actions interruptible with Escape, a HUD stop button, and a new user turn.
- Record every proposed and executed desktop action in the audit trail.
- Build first for Windows, matching the existing C# WebView2 shell and Win32 screen capture code.

## Non-goals

- No autonomous desktop actions without typed confirmation.
- Password and secret entry is warned about but not technically blocked.
- No elevation or administrator-rights escalation.
- No remote-control video protocol or browser extension in v1.
- No background automation that continues after the user leaves the turn.
- No multi-step desktop sequences in v1.

## Decisions made with the operator

- Launch policy: allow common apps, documents, folders, and `.lnk` shortcuts. Block `.exe`, `.bat`, `.ps1`, other scripts, and URLs in v1.
- Sensitive windows: filter and redact titles/processes matching password, banking, private, or incognito patterns from facts and target lists.
- Emergency stop: HUD Stop button plus the existing Ctrl+Alt+J global hotkey. Do not intercept Escape system-wide.
- Confirmation strength: typed `yes, do it` for every click, keystroke, launch, and file open.
- Card evidence: include a locally generated screenshot crop of the target area.
- Password entry: warn when a likely password/secret field is detected, but allow explicit approval to proceed.
- Sequence scope: one desktop action per user request in v1; multi-step chains are deferred.

## Constraints

- Existing screen capture is local and gated for cloud vision in `jarvis/vision/describe.py`.
- Existing window enumeration is Windows-only and includes visible titled windows, not minimized apps.
- `ToolRegistry` requires every tool to declare a tier matching `tiers.classify()`; unknown tools default to WRITE.
- Plugin-loaded tools must be READ and explicitly listed in `_EXTENSION_TOOL_NAMES`; non-READ tools must be builtins.
- The orb HUD already uses an always-on-top, borderless, non-activating WebView2 window in `orb/OverlayForm.cs`.
- Confirmation cards already exist conceptually through `write_confirm`/`edit_confirm` and WebSocket ops.
- Python backend hot reloading is not assumed; a desktop shell rebuild and backend restart may be needed after implementation.

## Approaches

### Approach 1: Agent tools only

Implement desktop operations as Python agent tools and require confirmation cards, but provide no desktop cursor overlay.

Pros: smallest change, reuses existing permission architecture.
Cons: the user cannot see where JARVIS is about to act; poor fit for the requested synchronized cursor.

### Approach 2: Confirmed tools plus desktop cursor overlay (chosen)

Implement the tools and a transparent cursor/HUD extension of `OverlayForm.cs`. Each action is proposed first, shown as a confirmation card, then executed only after approval. The overlay marks the target coordinate while the action is queued and animates while it executes.

Pros: matches the user's request, preserves explicit control, makes agent actions observable and stoppable.
Cons: requires coordinated Python, C#, and web UI changes; needs careful input-injection and focus handling.

### Approach 3: Full desktop streaming inside the orb

Stream the whole screen into the browser and drive it through an embedded remote desktop surface.

Pros: strong visual integration.
Cons: high latency, large implementation, greater privacy exposure, and unnecessary when local screen reading already exists.

Approach 2 is the recommended balance of capability, visibility, and safety.

## Architecture

### 1. Read-only system facts

Add a new first-party plugin module, likely `jarvis/plugins/system_facts.py`, exposed through the existing plugin loader.

Tools:

- `system_battery_status()`
  - percent, charging/discharging, plugged-in state, seconds remaining when available.
  - uses `psutil.sensors_battery()` with safe unavailable results.
- `system_network_status()`
  - interface names, addresses, up/down state, Wi-Fi/Ethernet indicators where available.
  - default routes and active connection names where obtainable through platform APIs.
  - no secrets, saved passwords, or full configuration dumps.
- `system_open_apps()`
  - visible window title, process name, PID, and window rectangle.
  - extends or complements `ScreenCapture.list_windows()`.
- `system_overview()`
  - compact combined answer for conversational questions such as "what is happening on my laptop?"

All are READ tier and must be added to `_EXTENSION_TOOL_NAMES` in `jarvis/security/tiers.py` if loaded as plugins. They must fail soft: unavailable telemetry returns an explicit unavailable field rather than guessing.

### 2. Desktop action tools

Add built-in tools because they are not READ-tier:

- `desktop_open_app(name_or_path)`
- `desktop_open_file(path)`
- `desktop_move_cursor(x, y, monitor=None)`
- `desktop_click(x, y, button="left", clicks=1)`
- `desktop_scroll(x, y, direction, amount)`
- `desktop_type(text, x=None, y=None)`
- `desktop_press_keys(keys, x=None, y=None)`

App/file targets are validated against the safe-target policy. Documents, folders, common applications, and `.lnk` shortcuts are allowed. `.exe`, `.bat`, `.ps1`, other scripts, and URLs are rejected in v1.

Each tool creates a proposal rather than executing immediately. A proposal contains:

- action ID
- action type
- target description
- coordinates or window title
- exact text/keys where applicable
- preconditions, such as focused window
- risk level
- expiry timestamp

Execution requires an approval token returned by the orb confirmation flow. A stale, altered, or already-used token must be rejected.

### 3. Confirmation and execution flow

```text
User request
  → agent proposes desktop action
  → backend validates tier and creates pending action
  → orb emits desktop_confirm card
  → user approves (or denies)
  → backend re-validates snapshot and token
  → overlay shows target marker / queued state
  → action executes
  → overlay shows done/stopped state
  → audit event and chat status emitted
```

Approval requires typed `yes, do it`. One card equals one action, and v1 allows one action per user request. Multi-step sequences are rejected with a clear message rather than partially executed.

For click/type actions, the card includes a locally generated screenshot crop of the target area and a concise screen description. Crops remain local unless cloud vision consent already applies.

Sensitive windows are filtered and redacted before they reach facts, proposals, or screenshots used in cards. Matching is based on configurable title/process patterns for password managers, banking, private browsing, and similar sensitive contexts.

### 4. Synchronized cursor overlay

Extend `orb/OverlayForm.cs` with a second transparent, always-on-top, click-through window layer:

- full-screen virtual-desktop bounds
- crosshair/ring at the target coordinate
- subtle path pulse from current OS cursor to target
- state colors: scanning, queued, acting, done, stopped
- compact label: "JARVIS: Click", "JARVIS: Type", etc.
- Stop button and Escape hotkey
- never steals focus; uses existing `WS_EX_NOACTIVATE`/`WS_EX_TOOLWINDOW` pattern

The Python backend sends overlay commands over WebSocket:

- `desktop_cursor_show`
- `desktop_cursor_move`
- `desktop_cursor_state`
- `desktop_cursor_hide`
- `desktop_hud_action`

The C# shell bridges these commands to the overlay layer. If the shell is not running, the orb still shows action cards and status, but desktop actions must fail closed rather than execute invisibly.

### 5. Emergency stop

A global cancellation token covers the entire pending/executing sequence.

Stop sources:

- HUD Stop button
- existing Ctrl+Alt+J global hotkey
- a new user turn
- approval timeout
- tool-level failure or target mismatch

When stopped mid-action:

- no further steps run
- cursor turns red and displays "Stopped"
- overlay hides after a short delay
- chat and audit record the cancellation

### 6. Windows input implementation

Use native Win32 APIs in a small first-party module, likely `jarvis/desktop/control.py`:

- `GetCursorPos` / `SetCursorPos`
- `SendInput` for mouse and keyboard events
- `GetForegroundWindow` / `SetForegroundWindow`
- `GetWindowRect` for window bounds
- `ShellExecuteW` or `os.startfile()` for approved app/file opening
- `psutil` for process and power telemetry

All OS calls sit behind an injectable interface so tests can use a fake desktop without moving the real pointer.

### 7. Security and privacy

- Desktop action tools are non-READ and excluded from plugin registration.
- Confirmation cards display the full concrete action, not a vague summary.
- Input text is truncated only for display; execution uses the exact approved text associated with the token.
- Likely password/secret fields trigger a prominent warning on the confirmation card, but explicit approval still allows typing.
- No action may be rewritten after approval.
- Screenshots and window titles may contain private content; they remain on-device unless vision consent already permits cloud analysis.
- Every proposal, approval, denial, execution, failure, and cancellation writes an audit event.
- Sensitive windows can be excluded by title/process policy, initially configurable in settings.

## Test strategy

Follow red-green-refactor with at least these regression areas:

- Tool exposure: read-only facts are registered; desktop actions are non-READ and require approval.
- Confirmation: proposal does not execute; stale or modified approval token is rejected.
- Telemetry: battery/network/window values are normalized and unavailable values are explicit.
- Cursor events: orb emits the documented overlay event sequence.
- Cancellation: Escape/stop/new-turn prevents subsequent execution.
- Win32 adapter: fake OS boundary verifies coordinates, clicks, keys, and focus behavior without real input.
- Privacy: desktop screenshots/crops do not go to cloud vision without existing consent.
- Audit: every state transition is recorded.

## Delivery order

1. Confirmation contract and pending-action model.
2. Read-only laptop facts tools and orb readback.
3. Open app/file with confirmation.
4. Cursor overlay protocol and HUD visual states.
5. Mouse/keyboard actions with confirmation and stop.
6. Audit, documentation, and full-suite verification.

## Open builder details

All scope, privacy, confirmation, and safety decisions are settled. Remaining choices are implementation details: exact C# class layout, local IPC transport, TTL duration, snapshot fields, DPI conversion, scroll units, key-name syntax, and audit event names.
