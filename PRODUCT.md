# Product

<!-- impeccable:product-schema 1 -->

## Platform

web — served as static HTML inside a native C# WebView2 shell (`orb/OrbForm.cs`), plus a second rendering of the same page (`?mode=hud`) as a topmost borderless floating HUD window.

## Users

- **Primary user (inferred, from code):** a single operator of a personal local voice assistant, at their desk, hands available (voice-first) or keyboard-free (HUD over their other work). Secondary: the same user reviewing agent work — trace inspection, tier policy, skill governance, audit chain — i.e. a power/debug user, not a general audience.

## Product Purpose

JARVIS is a phased personal voice assistant. The orb page is its **instrument surface**: it makes the assistant's live state (listening / thinking / speaking / muted) legible at a glance, carries typed fallback conversation, exposes settings (model preference, TTS voice, wake threshold, security tiers) live, and mirrors Phase 4 governance (skill ledger, hash-chained audit readback) without leaving the window. Success = the operator can always tell what the system is doing, what it is allowed to do, and what it did.

## Positioning

Local-first, privacy-bound: the voice pipeline (wake word, VAD, transcription, TTS fallback) and the memory stack (embeddings when Ollama is present) stay on the machine; conversational turns run on a configured hosted ladder (Gemini 3.5 → Agnes), and the model key never leaves the machine except to that provider. Security is a first-class, visible system — tier policies (READ / WRITE / DESTRUCTIVE), a pre-execution auditor, and a hash-chained audit trail are surfaced in the UI, not hidden behind it.

## Operating Context

Windows workstation. A Python orchestrator serves the page and owns the WebSocket pipeline (`ws://<host>:8010/ws`) plus REST endpoints. The native shell grants microphone access without a prompt and hands capture to exactly one surface at a time. HUD is toggled by hotkey (Ctrl+Alt+J). Phases: 1 console voice loop · 2 this orb GUI · 3 CLI worker · 4 security (tiers, auditor, audit chain, skill pipeline, RAG) · 5 floating HUD.

## Capabilities and Constraints

- **Live pipeline over WebSocket:** server ops `hello` (settings snapshot + model list trigger), `state` (idle / listening / thinking / speaking / muted), `chat`, `trace` (per-step latency + raw payload), `tts_done`, `error`, `settings_ok`; client ops `chat`, `settings`, `save_key`, `mute`, `start`/`stop` (manual listen), `audio` (base64 PCM), `playback_done`. Reconnect with backoff.
- **Audio:** mic via an inlined AudioWorklet (downsamples to 16 kHz Int16, 320-sample / 20 ms chunks); TTS arrives as binary chunks, plays through an `<audio>` element wired to an `AnalyserNode` (fftSize 64 → 26 frequency bins that drive the speaking animation). Playback must survive WebView2 autoplay policy (resume-on-gesture).
- **Native bridge (HUD only):** `window.chrome.webview.postMessage` ops `dragStart` / `dragEnd` / `hide-hud` / `open-full`; C# drives `window.jarvisHud.setMicEnabled(bool)` — the single-mic-owner contract.
- **REST readback:** `/api/ollama-models`, `/api/tiers`, `/api/skills`, `/api/audit` (loaded on connect and via "Refresh Phase 4 state").
- **Render constraint:** the orb is a 900-point Fibonacci-sphere particle render on a 2D canvas — no WebGL, no libraries, CPU-only; must stay cheap inside WebView2.
- **Security constraint (verbatim, from code):** "Agnes key present in .env — never leaves this machine except to Agnes." Sentry §6.7: user files never go through the sentry, only the normal write-tier confirm.

## Brand Commitments

- Name **JARVIS**; the interface is the "orb" — a particle-sphere that shifts character with the assistant's state.
- The Agnes key note and its phrasing are a product fact, kept as-is.
- State vocabulary: STANDBY / LISTENING / PROCESSING / SPEAKING / OFFLINE (HUD shows raw state names).

## Evidence on Hand

- `orb/static/index.html` — the incumbent single-file page (HTML + CSS + JS), all element IDs, WS ops, fetch endpoints and the orb renderer contract live here.
- `orb/OrbForm.cs` — WebView2 host, mic ownership, HUD window.
- `phase4.py`, `jarvis/security/launcher.py` — the secured-ask gate the tier settings mirror.
- Absent: no imagery, no external assets, no icon library — the page is fully self-contained by design (WebView2 offline tolerance).

## Product Principles

1. **Legible state** — the orb's animation must encode what the system is doing right now, at a glance, from across a desk.
2. **Visible security** — policy, ledger and audit are shown, not assumed; the UI mirrors the console gate live.
3. **Local-first trust** — nothing leaves the machine unless the operator explicitly configured it; say so in the UI when it matters.
4. **One surface, two bodies** — full window and floating HUD share one page and one mic owner; each must work at its size.
5. **Instrument, not toy** — it is a precision panel the operator watches; detail (latency, payloads, chains) is for inspection, not decoration.
