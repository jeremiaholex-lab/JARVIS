# JARVIS

**A local-first, voice-first personal AI assistant that can actually use your computer — and asks before it touches anything.**

JARVIS pairs a persistent conversational agent with real tool use, a visual desktop orb, durable background work, and a security model you can see. The default brain runs locally through Ollama; cloud escalation is optional and explicitly configured. Every action that changes your machine — writing a file, editing code, launching an app, moving the mouse — stops at a confirmation gate and waits for you.

The orb is the instrument surface: its color and motion show whether JARVIS is listening, thinking, speaking, or muted. The same browser page powers a full desktop window and a floating heads-up display (HUD), sharing one microphone owner and one agent backend.

| | |
| --- | --- |
| **Tools** | 38 available to the agent — files, documents, web, screen, browser, laptop facts, and 7 gated desktop actions |
| **Verification** | 888 tests passing, 5 skipped · 115/115 offline behavioral cases |
| **Default brain** | Local Ollama (`qwen3:1.7b`), with an optional Gemini → Lite → Agnes cloud ladder |
| **Voice** | Wake word → VAD → local Whisper transcription → hybrid hosted/local TTS |
| **Platform** | Windows 11 · Python 3.12+ · .NET WebView2 |

## What JARVIS can do

- Hold a persistent conversation with memory and semantic recall.
- Use tools for local files, project data, indexed documents, web search, Google and Notion reads, screenshots, window lists, browser actions, and time.
- Queue background work, create cron schedules, watch approved folders, and decompose multi-step goals into dependent tasks.
- Route each turn between a local Ollama model and a configurable cloud endpoint, with explicit slash-command model preference.
- Listen through wake-word detection, voice activity detection, and local transcription; speak through hybrid hosted CrowLLM/Gemini TTS with local Kokoro fallback.
- Propose writes and file edits through confirmation gates. Nothing is written until the operator confirms.
- Propose seven desktop actions (open app/file, move cursor, click, scroll, type, press keys) behind typed confirmation. Tool calls never perform OS input by themselves.
- Understand spoken app names — "open chrome", "launch code", "open my notes" resolve against your Start menu before a confirmation card is ever created.
- Answer read-only laptop questions about battery, network, and open applications.
- Expose traces, security tiers, skill state, and a hash-chained audit trail in the interface.
- Run a behavioral eval suite offline to check routing, tool use, memory, and safety decisions.

### Desktop control and live system facts

The seven desktop tools are `desktop_open_app`, `desktop_open_file`, `desktop_move_cursor`, `desktop_click`, `desktop_scroll`, `desktop_type`, and `desktop_press_keys`. All are `DESTRUCTIVE` built-ins: the agent may only propose one, and the tool call returns a pending card payload without moving the pointer, clicking, typing, scrolling, or launching anything. A card shows a safe preview and a local screenshot crop of the target area; the exact action travels in an approval snapshot.

Confirmation rules:

- Execution requires the typed phrase `yes, do it` and an exact match of the card snapshot.
- A proposal is one-shot; a stale, altered, repeated, or already-approved card fails closed.
- Only one desktop action is allowed per user request. Multi-step sequences and mixed batches are rejected, never partially executed.
- If the native overlay is not connected and sending a fresh heartbeat, no card is admitted and no input runs.
- A new user turn cancels the previous pending action. The HUD Stop button and `Ctrl+Alt+J` also cancel it. Escape is not intercepted globally.
- Sensitive window titles and processes (password managers, banking, private/incognito browsing) are hidden from facts, cards, target lists, and screenshot crops. A sensitive window vetoes capture entirely. Sensitive-sounding local document names stay exact in the approved target, but their card preview is redacted.
- Safe launch policy: documents, folders, common applications, and `.lnk` shortcuts are allowed. `.exe` (except common applications under Program Files), scripts, URLs/URI schemes, network paths, and alternate streams are rejected before a confirmation card exists and are revalidated immediately before execution.
- Entering text that looks like a password or secret shows a prominent warning on the card, but explicit typed approval still allows it.

### Spoken app names

`desktop_open_app` accepts the way people actually talk. Saying "open chrome" or "launch my notes" resolves to a real Start-menu shortcut **before** the confirmation card exists, so what you approve is exactly what launches.

Resolution runs in a fixed order, against a Start-menu index cached for ten minutes:

1. **Exact name** — a case-insensitive match on an installed shortcut.
2. **Alias** — a short spoken name mapped to its installed name (`chrome` → Google Chrome, `code` → Visual Studio Code, `notes` → Notepad).
3. **Fuzzy match** — a close spelling wins only if it clears a similarity cutoff and beats the runner-up by a clear margin, so `chrme` still finds Chrome but a genuinely ambiguous name does not guess.

An ambiguous or unknown name **fails closed**: no card is created, nothing is queued, and the reply lists the closest installed apps as suggestions ("Did you mean: Google Chrome?"). Suggestions are names only, never paths, so an error message can never smuggle a launchable target. Anything that looks like a path (`C:\...`, a `.lnk`, a UNC path) bypasses name resolution entirely and goes straight to the launch policy above.

Because resolution happens once, at proposal time, the executor runs the approved payload unchanged — there is no second lookup between approval and launch that could quietly change the target.

Add your own aliases under `app_aliases` in `orb/settings.json`; they merge over the defaults:

```json
{
  "app_aliases": {
    "browser": "firefox",
    "terminal": "windows terminal"
  }
}
```

Example prompts:

- "What is my battery status and what apps are open?" — read-only answer; no desktop action card.
- "Open chrome." — resolves to the Google Chrome shortcut, then a pending card; nothing opens until you type `yes, do it`.
- "Open Notepad for me." — a pending card; nothing opens until you type `yes, do it`.
- "Click the OK button at 640, 480 and tell me when it is done." — one card with a local crop; no input before approval.
- "Type `hello` at 300, 200." — one card; exact text is part of the snapshot.
- "Stop that desktop action." or `Ctrl+Alt+J` — cancels the pending action; it will not execute.

## Architecture

```text
Microphone or typed prompt
          |
          v
Browser orb / HUD  --WebSocket-->  Python orb server
 (HTML + JS)                     (orb/server.py)
                                   |
                                   v
                         Agent turn engine
                         (jarvis/agent/turn.py)
                          /      |       \
                   routing    tools    memory
                      |          |         |
               local/cloud   38 tools   SQLite + vectors
                                 |
                        task runner / schedules
                                 |
                    TTS event stream -> audio chunks
                                 |
                        orb / HUD speaks
```

### Main components

| Area | Location | Purpose |
| --- | --- | --- |
| Agent turn loop | `jarvis/agent/turn.py` | Routes the request, offers tool schemas, executes tool calls, feeds results back to the model, and retries when a model skips required tool use. |
| Tool registry | `jarvis/agent/registry.py` | Validates tool names, schemas, implementations, and security tiers. Extensions can register read-only tools only. |
| Plugin tools | `jarvis/plugins/` | Read-only laptop facts: battery, network, open apps, and system overview. Sensitive windows are filtered out before anything reaches the model. |
| Background work | `jarvis/agent/task_runner.py`, `schedules.py`, `file_watchers.py`, `planner.py` | Runs durable tasks, cron schedules, watched-folder jobs, and decomposed plans. |
| Memory and learning | `jarvis/agent/memory.py`, `semantic.py`, `user_model.py` | Stores episodes, vectors, and explicit user preferences. |
| Model providers | `jarvis/agent/providers.py`, `brain.py`, `routing.py` | Talks to Ollama and cloud APIs, manages preferences, and chooses a model per turn. |
| Security | `jarvis/security/` | Classifies actions, audits them, prompts for confirmation, and enforces typed approval for destructive edits. |
| Desktop control | `jarvis/desktop/` | The confirmed-action contract, launch policy, card evidence, app-name resolution, the Win32 input boundary, and the executor that revalidates before running. |
| Speech | `jarvis/speech.py`, `jarvis_voice_loop.py` | Shapes spoken text and drives the local wake/speech loop. |
| Orb backend | `orb/server.py` | FastAPI server, WebSocket pipeline, REST readback, settings, TTS streaming, and task endpoints. |
| Orb front end | `orb/static/index.html` | Single-file orb UI, transcript, composer, settings drawer, trace panel, and audio queue. |
| Native shell | `orb/*.cs` | WebView2 windows, microphone ownership, hotkey, and floating HUD. |
| Vendored runtime | `openjarvis_runtime/` | Pinned `OpenJarvis` submodule used by the Python runtime and tests. |
| Behavioral eval | `eval/` | Offline cases and optional live checks covering the shipping decision layers. |

### Agent loop

A turn is not a plain chat completion. The model receives tool schemas. If it returns a tool call, JARVIS executes that tool, appends the result to the conversation, and calls the model again. The final answer is therefore grounded in the tool result rather than guessed from training data. The loop also steers the model toward screen and web tools when the question depends on current state, and nudges it once if it answers such a question without using a tool.

## Security model

JARVIS uses explicit action tiers:

| Tier | Meaning | Confirmation |
| --- | --- | --- |
| `READ` | Reads local or connected data | Free by policy |
| `WRITE` | Changes stored project data or memory | One-tap confirmation |
| `DESTRUCTIVE` | Edits files or similarly hard-to-reverse actions | Typed `yes, do it` confirmation |

The auditor inspects actions before execution. If the optional language-model critic is unavailable or unreadable, the action is held rather than silently approved. Writes queue a token and change nothing until confirmed. File edits produce a diff first. The sentry auto-heal path patches a scratch copy of a skill and tests it in a sandbox before any candidate promotion; user files never go through the sentry.

### Loopback hardening

The orb backend binds `127.0.0.1:8010`, and the browser page it serves is the trusted client:

- **WebSocket gate.** `/ws` accepts loopback clients without an `Origin` (the CLI and test harness). Browsers always send one, so a browser must also be same-origin, present a loopback `Host` header (which defeats DNS rebinding, where the attacker's hostname stays in both fields), and supply the per-session task token the page receives at `/`. The token is a random `secrets.token_urlsafe(32)` minted at startup, compared in constant time.
- **Static confinement.** `/static/{path}` resolves the requested path and refuses anything outside `orb/static` — `..` segments and drive-absolute paths return 404.
- **Sandboxed skill tests.** The sentry's model-written test scripts pass through the same AST gate as `sandbox_execute`, and run in Docker isolation only; with Docker absent the test fails closed instead of falling back to the host user.

Secrets are ignored by Git. Do not commit `.env`, OAuth token files, API keys, model weights, or local databases.

## Requirements

- Windows 11 workstation for the native orb shell and HUD.
- Python 3.12 or newer for the backend.
- .NET SDK for the C# WebView2 shell.
- Ollama with a local model such as `qwen3:1.7b` for offline inference.
- Optional: a configured cloud API key for escalation and hosted TTS.
- Optional: connected Google, Notion, Telegram, or Microsoft 365 accounts for those specific tools.

## Getting started

### One-command install (Windows 11)

From a PowerShell prompt in the folder where you want JARVIS installed, run:

```powershell
irm https://raw.githubusercontent.com/jeremiaholex-lab/JARVIS/master/scripts/install.ps1 | iex
```

The installer checks that Git, Python 3.12+, .NET SDK, winget, and Ollama are present (printing a download link for anything missing); clones the public repository; installs the Python runtime into the vendored submodule's virtual environment; sanity-checks that the test suite still collects; pulls `qwen3:1.7b`; and asks for optional API keys with links to the relevant provider pages. Leave any prompt blank to skip it; supplied values are merged into the gitignored `.env` and existing keys are preserved on re-runs.

If you prefer to review the installer before running it, download and inspect [`scripts/install.ps1`](scripts/install.ps1) first, then run:

```powershell
.\scripts\install.ps1
```

### Manual setup

#### 1. Get the source and pinned runtime

```powershell
git clone https://github.com/jeremiaholex-lab/JARVIS.git
cd JARVIS
git submodule update --init --recursive
```

#### 2. Install Python dependencies

This checkout uses a local Python runtime environment. Install the packages required by the vendored OpenJarvis runtime and JARVIS, then confirm pytest can import both trees:

```powershell
python -m pytest --collect-only -q
```

`conftest.py` adds the project root, `openjarvis_runtime/src`, and `orb` to the import path, so a plain pytest run works without activating a specific virtual environment.

### Start the orb

From the repository root:

```powershell
cd orb
.\launch_orb.bat
```

The shell host starts the backend on `127.0.0.1:8010` if nothing is serving there yet and owns its lifetime. To start only the floating HUD:

```powershell
.\launch_orb.bat hud
```

Press `Ctrl+Alt+J` to toggle the HUD.

### 4. Verify the backend

Open `http://127.0.0.1:8010/` in a browser. The page should connect over WebSocket and report a live state. The Advanced drawer shows voice status, model settings, and readback for tasks, tiers, skills, and audit state.

## Using JARVIS

### Typed and voice turns

Type into the composer, or use the microphone controls for a voice turn. Full answer text appears as soon as the model produces it; audio follows as it is generated. If hosted speech is unavailable, JARVIS falls back to the local voice and says which voice was used.

### Model slash commands

Slash commands change preference. The backend still chooses the actual model and may fall back.

| Command | Preference |
| --- | --- |
| `/3.5`, `/flash` | Gemini 3.5 Flash |
| `/lite`, `/3.5 lite`, `/2.5` | Gemini Lite |
| `/agnes`, `/cloud`, `/api` | Agnes cloud endpoint |
| `/auto`, `/default`, `/chain`, `/gemini` | Automatic ladder |
| `/help` | Print the command help |

A slash must be the first token. `what is /lite` is an ordinary question, not a model switch.

### Voice settings

The Advanced drawer exposes four trade-offs:

- **Voice source:** automatic, hosted, or local.
- **Voice pacing:** quick start or smoother gapless audio.
- **Spoken length:** bounds only what is spoken; full text stays on screen.
- **Speech rate:** changes playback speed.

Local Kokoro is private and offline but slower on CPU. Hosted speech uses less local CPU and starts smoother, but sends reply text to an external provider. Automatic mode prefers CrowLLM when it is configured, then Gemini, then local Kokoro. CrowLLM uses the configured `tts-bruce-en` voice through an OpenAI-compatible speech endpoint.

Configure CrowLLM with `CROWLLM_API_KEY` and optionally `CROWLLM_BASE_URL` in the gitignored local environment. The model and voice live under `crowllm_tts` in `orb/settings.json`. Never commit the key. If a hosted provider fails, is rate limited, or has no key, JARVIS falls back to the local voice. Gemini quota rejections also trigger a temporary cooldown in automatic mode; explicitly selecting Gemini overrides that cooldown, while explicitly selecting CrowLLM always targets CrowLLM.

### Background work

Ask for a delayed task, a repeating schedule, or a folder watcher in plain language. JARVIS uses `jarvis_task_enqueue`, `jarvis_task_schedule`, `jarvis_plan_and_enqueue`, and `jarvis_file_watch` where appropriate. Background work is read-only; it cannot promise unattended writes or external actions.

## Configuration

Runtime settings live in `orb/settings.json`, including local model, cloud endpoint, tier policies, voice, wake threshold, voice activity detection tail, follow-up timing, barge-in sensitivity, speech rate, context size, vision consent, and `app_aliases` for spoken app names. Cloud keys are read from the environment or local secret files and must never be committed.

`jarvis-phase3.toml` and `jarvis-phase4.toml` are older phase configuration examples. Treat `orb/settings.json` as the live orb configuration.

## REST and WebSocket surface

The orb backend exposes:

- `GET /` and `/static/{path}` for the browser UI.
- `WS /ws` for turns, state, traces, settings, TTS events, barge-in, and playback acknowledgements.
- `GET /api/status`, `/api/ollama-models`, `/api/model`, `/api/wake`, and `/api/agent` for live state.
- `GET` and `POST /api/tasks`, plus cancel and retry endpoints.
- `GET` and `POST /api/watchers`, plus watcher actions.
- `GET /api/tiers`, `/api/skills`, `/api/audit`, and `/api/alerts` for governance readback.
- `POST /api/shutdown` and `/api/alerts/acknowledge` for control operations.

## Verification

Run the full first-party test suite:

```powershell
python -m pytest
```

Run the offline behavioral eval:

```powershell
python eval/run_eval.py
```

Run the live eval only while the orb is running:

```powershell
python eval/run_eval.py --live
```

Latest recorded results on `master`: **888 first-party tests passed, 5 skipped**; **115/115 offline behavioral cases passed**. CI checks are not configured yet, so these are local verification results.

## Troubleshooting

**The orb opens but never connects.** Confirm `127.0.0.1:8010` serves the page. Check `orb_shell.out` and `orb_shell.err` for backend startup errors.

**The model says it cannot use a tool.** Confirm the tool appears in the Advanced or skill readback. Re-run the offline eval; its turn and nudge cases pin the tool-call contract.

**Speech starts late.** Open Advanced and check the voice note. If hosted TTS is in quota cooldown, JARVIS uses local Kokoro. Quick pacing starts audio sooner; gapless pacing waits longer to avoid pauses.

**A write did not happen.** Writes only queue a confirmation token until you approve them. Destructive edits require the exact phrase `yes, do it`.

**Imports fail under pytest.** Run `git submodule update --init --recursive` and confirm `openjarvis_runtime/src/openjarvis` exists.

## Repository layout

```text
jarvis/               Python agent, tools, security, memory, speech, skills
jarvis/desktop/       Confirmed desktop actions, launch policy, app resolution
jarvis/plugins/       Read-only laptop-facts tools
orb/                  FastAPI backend, browser UI, C# WebView2 shell
eval/                 Behavioral eval cases and runner
docs/superpowers/     Design specs and implementation plans
openjarvis_runtime/   Pinned OpenJarvis Git submodule
test_*.py             First-party pytest modules
tests/                Additional first-party pytest modules
```

## Releases

Releases are tagged on `master` and published on the [Releases page](https://github.com/jeremiaholex-lab/JARVIS/releases). Each release lists what shipped, the verification results at that point, and any upgrade notes.

| Version | Focus |
| --- | --- |
| [v0.1.0](https://github.com/jeremiaholex-lab/JARVIS/releases/tag/v0.1.0) | First tagged release: voice-first agent, gated desktop control with spoken app names, durable tasks, orb UI and HUD |

This project follows a simple convention: a tag marks a state of `master` where the full test suite and the offline eval both pass. There is no separate release branch.

## Contributors

JARVIS is built and maintained by **[jeremiaholex-lab](https://github.com/jeremiaholex-lab)** — product direction, design, and the operating decisions behind the security model.

Implementation is pair-programmed with Claude (Anthropic) through [Claude Code](https://claude.com/claude-code). Commits carry a `Co-Authored-By` trailer naming the model that worked on them, so the history records both the human decision and the assistant that helped carry it out.

Contributions are welcome. Before opening a pull request:

1. Run `python -m pytest` — the full suite must pass.
2. Run `python eval/run_eval.py` — the offline behavioral cases must pass.
3. Keep new tools honest about their tier: extensions may register **read-only** tools only, and anything that changes the machine belongs behind a confirmation gate.
4. Never commit secrets, model weights, local databases, or `.env` files.

See [PRODUCT.md](PRODUCT.md) for the product commitments a change should respect, and [DESIGN.md](DESIGN.md) for the interface principles.

## Further reading

- [PRODUCT.md](PRODUCT.md) explains the product purpose, operating context, and brand commitments.
- [DESIGN.md](DESIGN.md) documents the orb interface, layout, voice output, and interaction principles.
- [CODEBASE-ANALYSIS.md](CODEBASE-ANALYSIS.md) records the earlier deep architecture review and historical findings.
- [docs/superpowers/specs](docs/superpowers/specs) contains design specifications for roles, schedules, plugins, sandboxing, and voice-first turns.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
