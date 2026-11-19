# JARVIS build handoff — prompts for Claude Code

How to use: run Claude Code inside this folder (`C:\Users\HP\Desktop\JARVIS`),
one phase at a time, in order. Only start the next phase when the previous
phase's done-test genuinely passes. If a phase gets stuck, stop and come
back — do not let it "finish" a phase by skipping its done-test.

Two early-check items from the design doc (§11) that Claude Code should
verify during Phase 1:
- whether WSL2 + Docker will coexist with the 8B local model in ~12 GB
  usable RAM (a quick check only — it only matters for Phase 4)
- Agnes AI is already resolved: base URL and model name are in the design
  doc §12 and the `.env` file in this folder — no follow-up question needed

---

## Phase 1 prompt — chassis + local voice loop

```
Read docs/jarvis-agent-design.md fully, especially sections 1, 5, 6.1, 6.2,
6.3, 6.4 and section 9 Phase 1.

Your task is Phase 1 only: get the local voice loop working.
1. Install OpenJarvis on this Windows machine (one-line installer or manual;
   uv + Ollama + starter model, ~3 min on a good connection).
2. Run an 8B 4-bit GGUF model locally via Ollama on CPU as the routine-task
   brain. Target ~10-20 tok/s; if the machine can't hit that, report the
   measured speed before choosing a smaller model.
3. Wire Kokoro-82M (pip kokoro + espeak-ng on Windows) as the TTS backend
   for agent replies.
4. Add the input side: faster-whisper (small) for STT, a wake word
   (openWakeWord or Porcupine), and Silero VAD for end-of-speech.
   Text input via console is fine alongside it; the ORB is NOT in scope
   for this phase.
5. Configure the Agnes AI OpenAI-compatible endpoint as the escalation
   cloud engine: base URL and API key are in this folder's `.env` file
   (gitignored); confirmed model is `agnes-3.0-flash` (design doc §12).
   Keep the key out of committed code.

Done-test for this phase: I say the wake word, ask it something routine,
and it replies with a spoken answer, fully on-device (Ollama + Kokoro),
while a harder question transparently escalates to Agnes AI and still gets
a spoken reply. Also: while doing the install, run a quick memory
projection (OS + 8B model + Whisper + Kokoro + WSL2/Docker) and tell me
whether Phase 4's sandbox will fit in my ~12 GB usable RAM.

Do NOT start Phase 2. When the done-test passes, tell me exactly what is
running, what it costs in RAM, and the measured LLM speed.
```

## Phase 2 prompt — the 3D orb GUI

```
Read docs/jarvis-agent-design.md sections 6 (especially 6.1-6.4) and 7.
Phase 1 is complete: the local voice loop works (Ollama 8B + Kokoro TTS +
Whisper/wake-word/VAD).

Your task is Phase 2 only: build the orb window.
- Use the locked design in orb-reference.html (3D Holo-Flux: perspective
  projection, depth-sorted particle shell, 3D hex frames, drag-to-orbit,
  scroll-to-zoom, driven by audio-pipeline state — idle/listening/thinking/
  speaking/muted).
- Desktop shell: your call (Electron / Tauri / WebView2) per design doc §8,
  but optimize for running as a lightweight background service on Windows.
- Wire the orb's states to the real pipeline: listening = live mic energy,
  speaking = Kokoro output energy, thinking = internal pulse, muted = grey.
- Also include the chat history, per-step execution trace cards (latency +
  expandable raw payload, Brahma-style "Inspect"), and the Settings hub
  (local model choice, Agnes AI endpoint, permission-tier defaults, API keys)
  alongside the orb.

Done-test: I have a single desktop window where the orb visibly reacts to
every agent state with no lag I can notice, and I can complete one full
voice conversation entirely through this window. Keep the 2D-canvas 3D
approach — no WebGL libraries, must run CPU-only on my laptop.
```

## Phase 3 prompt — the three day-to-day jobs

```
Read docs/jarvis-agent-design.md sections 6.1, 6.5, 6.9 and 9 (Phase 3).
Phase 2 is complete (orb GUI + voice loop working).

Your task is Phase 3 only:
1. Morning voice briefing: connect Google (Gmail/Calendar/Tasks via the
   GDrive OAuth from OpenJarvis's connectors) and make the morning_digest
   agent produce a spoken daily digest — email, calendar, tasks — using
   Kokoro, on a schedule AND on demand.
2. Code assistant: point OpenJarvis's CodeAct/openhands agent at my project
   folders (I'll tell you which) and confirm it can read, modify and run
   code and tests inside them with the permission tiers from design §6.7
   (read free, write confirmed, destructive hard-gated).
3. Project + deadline tracking: a local store (SQLite) of active projects
   and deadlines that the agent reads and updates, with the proactive
   "task X is due in <48h and still open" trigger from §6.9.

Done-test: at my chosen morning time, the agent speaks my digest out loud;
I ask it to fix a bug in one of my projects and it does so through the
confirm-before-write flow; when I add a deadline to the store, it
proactively surfaces it when within 48h.
```

## Phase 4 prompt — the sophistication layer

```
Read docs/jarvis-agent-design.md sections 6.6, 6.7, 6.8, 6.10 and 9
(Phase 4). Phases 1-3 are complete.

Your task is Phase 4, in this order (safety first):
1. Permission tiers fully enforced across all agent actions (read free /
   write confirmed / destructive hard-gated, all reversible) + the
   auditor/critic agent that reviews planned actions BEFORE execution.
2. Safe self-evolving skills: agent-proposed skills in agentskills.io
   format, sandbox-tested in Docker-on-WSL2 (memory-capped ~4 GB, no
   network access to my accounts), promoted only after tests pass AND I
   approve. If the Phase-1 memory check showed Docker won't fit, build
   the restricted-process fallback from §6.6 instead and say so.
3. Auto-heal sentry: background watcher that diagnoses agent runtime
   failures with the LLM and patches agent skill code inside the sandbox
   only — never my files.
4. RAG: local embedding model + SQLite vector store over the folders I
   choose, indexed at setup; agents search it during turns.

Done-test: I watch the auditor hold a risky action for confirmation; the
agent forges a brand-new skill for a capability it lacked, it passes its
sandbox tests, it asks me to approve, and after approval I can invoke it
next session; it answers a question that only exists in one of my indexed
documents, citing it.
```

## Phase 5 prompt — floating overlay mode (only if/when I ask)

```
Read docs/jarvis-agent-design.md section 7. v1 is stable.
Your task: add the always-on-top floating orb overlay (true HUD mode)
alongside the dedicated window — multi-monitor aware, no focus-stealing
when not addressed, wake-word still works with the overlay in front of any
app. This is explicitly deferred work; only start when I say so.
```
