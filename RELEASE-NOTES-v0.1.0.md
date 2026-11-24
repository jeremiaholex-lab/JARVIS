First tagged release of JARVIS — a local-first, voice-first personal AI assistant for Windows that can use your desktop, and asks before it touches anything.

## What's in this release

### Voice-first conversation
- Wake-word detection (openWakeWord) → voice activity detection (Silero) → local transcription (faster-whisper), with barge-in so you can interrupt a reply mid-sentence.
- Hybrid speech output: hosted CrowLLM/Gemini TTS with automatic fallback to local Kokoro, and the UI always says which voice actually spoke.
- Follow-up windows keep the floor open after a reply, so a conversation continues without re-saying the wake word.
- Reopening the window restores the stored transcript instead of showing an empty panel.

### Desktop control behind typed confirmation
Seven desktop tools — open app, open file, move cursor, click, scroll, type, press keys — are all `DESTRUCTIVE` built-ins. A tool call only proposes: it returns a pending card and performs no OS input. Execution requires the typed phrase `yes, do it` and an exact match of the card snapshot.

- One action per request; multi-step sequences and mixed batches are rejected rather than partially executed.
- Proposals are one-shot. A stale, altered, repeated, or already-approved card fails closed.
- A launch policy rejects `.exe` (outside common Program Files apps), scripts, URLs/URI schemes, network paths, and alternate streams **before** a card exists, and revalidates immediately before execution.
- Sensitive windows (password managers, banking, private browsing) are hidden from facts, cards, target lists, and screenshot crops; a sensitive window vetoes capture entirely.
- New in this release: **spoken app names**. "Open chrome", "launch code", and "open my notes" resolve against the Start menu at proposal time — exact name, then alias, then a conservative fuzzy match. Ambiguous or unknown names fail closed with suggestions instead of guessing. Aliases are configurable via `app_aliases` in `orb/settings.json`.

### Agent and tools
- 38 tools available to the agent: local files, project data, indexed documents, web search, Google and Notion reads, screenshots, window lists, browser actions, time, laptop facts, and the seven gated desktop actions.
- A real tool loop: the model receives schemas, JARVIS executes the call, feeds the result back, and answers from the result rather than from training data.
- Persistent memory with semantic recall, plus explicit user preferences.
- Durable background work: queued tasks, cron schedules, watched folders, and decomposed multi-step plans. Background work is read-only.
- Model routing between local Ollama and an optional Gemini → Lite → Agnes cloud ladder, with slash-command preference (`/flash`, `/lite`, `/agnes`, `/auto`).

### Interface
- A single-file browser orb UI with transcript, composer, settings drawer, trace panel, and audio queue.
- Native WebView2 shell and a floating HUD sharing one microphone owner and one backend; `Ctrl+Alt+J` toggles the HUD.
- Live governance readback: security tiers, skill state, traces, and a hash-chained audit trail.

## Security model

| Tier | Meaning | Confirmation |
| --- | --- | --- |
| `READ` | Reads local or connected data | Free by policy |
| `WRITE` | Changes stored project data or memory | One-tap confirmation |
| `DESTRUCTIVE` | Edits files or similarly hard-to-reverse actions | Typed `yes, do it` |

Extensions may register read-only tools only. If the optional model critic is unavailable or unreadable, an action is held rather than silently approved.

## Verification

- **880 first-party tests passed, 5 skipped** (`python -m pytest`)
- **115/115 offline behavioral cases passed** (`python eval/run_eval.py`)

CI is not configured yet; these are local verification results on `master` at this tag.

## Requirements

Windows 11 · Python 3.12+ · .NET SDK for the WebView2 shell · Ollama with a local model such as `qwen3:1.7b`. Cloud keys, hosted TTS, and connected Google/Notion accounts are optional.

## Getting started

```powershell
git clone https://github.com/jeremiaholex-lab/JARVIS.git
cd JARVIS
git submodule update --init --recursive
```

Then install the Python dependencies and start the orb from `orb\launch_orb.bat`. Full setup, configuration, and troubleshooting are in the [README](https://github.com/jeremiaholex-lab/JARVIS#readme).

## Notes

- This release was tagged while the repository was private; it has since been made public (2026-11-24) under the Apache-2.0 license.
- Never commit `.env`, OAuth tokens, API keys, model weights, or local databases.

🤖 Generated with [Claude Code](https://claude.com/claude-code)