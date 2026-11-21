# JARVIS

JARVIS is a local-first, voice-first personal AI assistant. It combines a persistent conversational agent, real tool use, a visual desktop orb, removable background work, and a visible security model. The default brain runs locally through Ollama; cloud escalation is optional and explicitly configured.

The orb is the instrument surface: its color and motion show whether JARVIS is listening, thinking, speaking, or muted. The same browser page powers a full desktop window and a floating heads-up display (HUD), sharing one microphone owner and one agent backend.

## What JARVIS can do

- Hold a persistent conversation with memory and semantic recall.
- Use tools for local files, project data, indexed documents, web search, Google and Notion reads, screenshots, window lists, browser actions, and time.
- Queue background work, create cron schedules, watch approved folders, and decompose multi-step goals into dependent tasks.
- Route each turn between a local Ollama model and a configurable cloud endpoint, with explicit slash-command model preference.
- Listen through wake-word detection, voice activity detection, and local transcription; speak through hybrid hosted Gemini TTS with local Kokoro fallback.
- Propose writes and file edits through confirmation gates. Nothing is written until the operator confirms.
- Expose traces, security tiers, skill state, and a hash-chained audit trail in the interface.
- Run a behavioral eval suite offline to check routing, tool use, memory, and safety decisions.

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
               local/cloud   26 tools   SQLite + vectors
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
| Background work | `jarvis/agent/task_runner.py`, `schedules.py`, `file_watchers.py`, `planner.py` | Runs durable tasks, cron schedules, watched-folder jobs, and decomposed plans. |
| Memory and learning | `jarvis/agent/memory.py`, `semantic.py`, `user_model.py` | Stores episodes, vectors, and explicit user preferences. |
| Model providers | `jarvis/agent/providers.py`, `brain.py`, `routing.py` | Talks to Ollama and cloud APIs, manages preferences, and chooses a model per turn. |
| Security | `jarvis/security/` | Classifies actions, audits them, prompts for confirmation, and enforces typed approval for destructive edits. |
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

Secrets are ignored by Git. Do not commit `.env`, OAuth token files, API keys, model weights, or local databases.

## Requirements

- Windows 11 workstation for the native orb shell and HUD.
- Python 3.12 or newer for the backend.
- .NET SDK for the C# WebView2 shell.
- Ollama with a local model such as `qwen3:1.7b` for offline inference.
- Optional: a configured cloud API key for escalation and hosted Gemini TTS.
- Optional: connected Google, Notion, Telegram, or Microsoft 365 accounts for those specific tools.

## Getting started

### 1. Get the source and pinned runtime

```powershell
git clone https://github.com/jeremiaholex-lab/JARVIS.git
cd JARVIS
git submodule update --init --recursive
```

The repository is private, so GitHub will ask for account access.

### 2. Install Python dependencies

This checkout uses a local Python runtime environment. Install the packages required by the vendored OpenJarvis runtime and JARVIS, then confirm pytest can import both trees:

```powershell
python -m pytest --collect-only -q
```

`conftest.py` adds the project root, `openjarvis_runtime/src`, and `orb` to the import path, so a plain pytest run works without activating a specific virtual environment.

### 3. Start the orb

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

Local Kokoro is private and offline but slower on CPU. Hosted Gemini TTS starts smoother and uses less local CPU, but requires a working key and sends reply text to an external provider.

### Background work

Ask for a delayed task, a repeating schedule, or a folder watcher in plain language. JARVIS uses `jarvis_task_enqueue`, `jarvis_task_schedule`, `jarvis_plan_and_enqueue`, and `jarvis_file_watch` where appropriate. Background work is read-only; it cannot promise unattended writes or external actions.

## Configuration

Runtime settings live in `orb/settings.json`, including local model, cloud endpoint, tier policies, voice, wake threshold, voice activity detection tail, follow-up timing, barge-in sensitivity, speech rate, context size, and vision consent. Cloud keys are read from the environment or local secret files and must never be committed.

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

Latest recorded results in this repository's working history: 634 first-party tests passed, 6 skipped; 105/105 offline behavioral cases passed. CI checks are not configured yet, so these are local verification results.

## Troubleshooting

**The orb opens but never connects.** Confirm `127.0.0.1:8010` serves the page. Check `orb_shell.out` and `orb_shell.err` for backend startup errors.

**The model says it cannot use a tool.** Confirm the tool appears in the Advanced or skill readback. Re-run the offline eval; its turn and nudge cases pin the tool-call contract.

**Speech starts late.** Open Advanced and check the voice note. If hosted TTS is in quota cooldown, JARVIS uses local Kokoro. Quick pacing starts audio sooner; gapless pacing waits longer to avoid pauses.

**A write did not happen.** Writes only queue a confirmation token until you approve them. Destructive edits require the exact phrase `yes, do it`.

**Imports fail under pytest.** Run `git submodule update --init --recursive` and confirm `openjarvis_runtime/src/openjarvis` exists.

## Repository layout

```text
jarvis/               Python agent, tools, security, memory, speech, skills
orb/                  FastAPI backend, browser UI, C# WebView2 shell
eval/                 Behavioral eval cases and runner
docs/superpowers/     Design specs and implementation plans
openjarvis_runtime/   Pinned OpenJarvis Git submodule
test_*.py             First-party pytest modules
tests/                Additional first-party pytest modules
```

## Further reading

- [PRODUCT.md](PRODUCT.md) explains the product purpose, operating context, and brand commitments.
- [DESIGN.md](DESIGN.md) documents the orb interface, layout, voice output, and interaction principles.
- [CODEBASE-ANALYSIS.md](CODEBASE-ANALYSIS.md) records the earlier deep architecture review and historical findings.
- [docs/superpowers/specs](docs/superpowers/specs) contains design specifications for roles, schedules, plugins, sandboxing, and voice-first turns.
