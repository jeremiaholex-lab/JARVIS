# JARVIS — Local-First Desktop AI Agent — Design

Date: 2026-11-06
Status: planning (not yet built)
Target machine: HP laptop, 16–18 GB RAM, integrated graphics only, Windows 11

## 1. What this is and why

A JARVIS-style autonomous desktop assistant that lives on the user's own laptop:
it hears you, thinks, talks back, and does real work — file work, code work,
calendar/project tracking — with everything privacy-critical staying on-device.
The cloud is touched only when a task is too hard for the local model, and
only for the duration of that task.

The "JARVIS" requirement is specific: a continuous voice loop (wake word →
listen → think → speak) with a visible 3D orb whose motion is driven by the
audio pipeline, not a chat box.

## 2. Goals

- **Morning voice briefing** — daily spoken digest: email, calendar, tasks,
  projects and deadlines; plays on wake or on a schedule.
- **Hands-on code help** — read/modify/debug files in the user's projects,
  run tests, explain diffs.
- **Project and deadline tracking** — the agent remembers active projects and
  upcoming deadlines and proactively surfaces them ("you have X due Friday").
- **General voice conversation** — ask anything, natural turn-taking, no
  cloud dependency for the loop itself.
- **Local-first privacy** — memory, transcripts, embeddings never leave the
  machine unless the user explicitly approves escalation.

## 3. Non-goals

- Not a cloud SaaS product; not meant for redistribution or commercial use.
- No NVIDIA-GPU-dependent features as a hard requirement (integrated
  graphics is the design target; dGPU is optional acceleration only).
- Not replacing an IDE — code help means agent-level file/command work, not
  a full editor.

## 4. Anti-goals (what would count as failure)

- The voice loop feels laggy or drops out mid-sentence on a 16 GB laptop.
- The agent can't be stopped, corrected, or audited while it acts.
- Memory or credentials leak to the cloud by accident.
- The "self-evolving skills" feature writes untested code into the running
  agent without a gate.

## 5. Hardware constraint (settled)

- 16–18 GB shared RAM, integrated graphics. Practical model budget after
  OS overhead: ~12 GB.
- Design ceiling: **8B LLM at Q4 (≈5 GB)** on CPU via a llama.cpp-style
  runtime (~10–20 tok/s expected on a modern laptop CPU). This is the
  *routine-task* brain.
- **Hybrid routing**: hard reasoning escalates to a cloud model. Local-first
  by default, cloud only when the local model can't handle it — this is
  OpenJarvis's native philosophy and makes local-first practical rather
  than just private.
- **Escalation target: Agnes AI (user's choice, replacing Gemini — cited
  reason: Gemini's free quota is easily exhausted). Agnes AI exposes an
  OpenAI-compatible API at `https://apihub.agnes-ai.com/v1`. Confirmed
  escalation model: `agnes-3.0-flash` (verified working 2026-11-06).
  Credentials live in the project `.env` file (gitignored) — never in
  committed code or this document.**
- Everything else fits easily at this size: Kokoro-82M TTS (<100 MB, CPU),
  small Whisper/faster-whisper STT (real-time on CPU), a local embedding
  model (~1 GB) + a lightweight SQLite-backed vector store.
- The full voice loop (wake word + STT + small LLM + Kokoro) runs entirely
  in RAM; no GPU required anywhere.

## 6. The stack, layer by layer

### 6.1 Chassis / brain — OpenJarvis
Stanford (Hazy Research) `open-jarvis/OpenJarvis` (Apache 2.0). Provides:

- The agent runtime: eight built-in agents across on-demand / scheduled /
  continuous modes — including `morning_digest` (our daily briefing),
  `deep_research`, a long-horizon `monitor` agent, `orchestrator`, and a
  CodeAct/`openhands` agent (the code-assistant job).
- **Skills system** following the open `agentskills.io` standard — install
  from ~150 Hermes skills, ~13,700 community skills, or any repo; skills are
  optimizable from the user's own trace history (DSPy-based).
- Local model routing (Ollama out of the box; cloud engines when needed).
- OAuth connectors (**Google: Gmail/Calendar/Tasks via GDrive OAuth — confirmed
  provider**) — feeds the morning digest and project tracking.
- Windows install: one-line PowerShell installer; runs as a scheduled-task
  service, which suits a laptop that's often in sleep/lock.
- A local API server + browser GUI (used for early versions; see §7 for the
  orb GUI that replaces it long-term).

**Why not Brahma as the chassis:** Brahma's license is explicitly
personal-use-only, no redistribution/commercial use. We can't vendor its
code into this project. OpenJarvis is Apache 2.0 and is the *framework*,
not a finished app — it's the right foundation.

### 6.2 Voice out — Kokoro-82M
`hexgrad/kokoro` + `Kokoro-82M` on HuggingFace (Apache-licensed weights).
82M-parameter TTS, pip-installable, CPU-friendly, multiple voices. Slots in
as OpenJarvis's TTS backend — the `morning_digest` agent already produces
TTS audio; we wire Kokoro into that pipeline and into every agent turn.

### 6.3 Voice in — local STT + wake word + VAD
- **Whisper (small, via faster-whisper or whisper.cpp)** — transcribes the
  user on CPU in real time.
- **openWakeWord or Porcupine** — a "Hey [agent]" hotword so the user isn't
  transcribing 24/7 (battery + privacy).
- **Silero VAD** — end-of-speech detection for natural turn-taking.
- These three make the voice loop fully local: wake word → record →
  VAD-slice → Whisper → LLM.

### 6.4 Core LLM — 8B @ Q4 on CPU + cloud escalation
- llama.cpp-compatible runtime (Ollama is OpenJarvis's default and exposes
  the same model). 8B Q4_K_M (~4.5–5 GB) is the floor for conversational
  speed on CPU; 13–14B is out of comfortable range without a dGPU.
- A routing policy in OpenJarvis decides local vs. cloud per turn. Default
  is local; escalation triggers are: tool-heavy multi-step plans the small
  model struggles with, explicit user request ("think hard about this"),
  or a local-model confidence check.

### 6.5 Memory — three tiers, all local
- **Episodic** — what happened, when; stored in OpenJarvis's trace store,
  retrievable for context.
- **Semantic** — durable facts about the user, their projects, preferences;
  written deliberately (agent decides what's worth keeping, with a
  consolidation pass, not just append).
- **User model** — a rolling summary of who the user is, their projects,
  deadlines, style; updated as new facts arrive.
- **Vector store** — SQLite-backed, local embedding model (~1 GB), for
  RAG over the user's own documents (§6.8).

All memory lives on-disk on the machine. Nothing in the memory system is
cloud-synced by default.

### 6.6 Self-evolving skills — done safely
Brahma's "Project Ultron" idea is the right instinct: the agent notices a
capability gap, writes a new tool, tests it, and registers it. The
sophisticated version we build:

1. Agent proposes a new skill (as an `agentskills.io`-format skill, so it
   slots into OpenJarvis's existing catalog).
2. The skill runs in a **sandbox: Docker on WSL2, memory-capped (~4 GB) —
   builder's call, best fit for a 16GB iGPU laptop**: real isolation with
   no access to the user's real files or credentials, mature and
   scriptable for a fast test loop, and the cap keeps it from starving the
   local LLM. (Fallback if WSL2/Docker proves too heavy day-to-day: a
   restricted-process sandbox behind the same gate — the approval gate is
   what protects the user; the container is what makes it trustworthy.)
   The skill is run against a small spec/test set the agent also proposes,
   with no network access to the user's accounts.
3. Only when tests pass does the skill reach a **human approval gate** —
   the user sees what the skill does, what it can touch, and approves or
   rejects before it's registered into the live catalog.
4. OpenJarvis's trace-based skill optimization (DSPy) can then improve the
   skill from actual usage, again behind the same gate.

This is the "sophistication" upgrade over Brahma's version, which hot-reloads
without a test-or-approve step.

### 6.7 Computer use + vision, with permission tiers
- Screen perception: a VLM (local if it fits in the remaining RAM budget,
  cloud escalation if not) can read the current screen on request — "look
  at this error," "what's on this page."
- UI automation: click/type/file actions, always through the permission tiers
  below, never raw OS access.
- **Permission tiers** (this is the "can't be stopped or corrected" anti-goal
  solved):
  - **Read-only** actions (read a file, look at the screen, check the
    calendar): free, no prompt.
  - **Write** actions (edit a file, add a calendar entry, send a message):
    shown to the user with a one-tap confirm before executing, and all are
    reversible (the agent keeps an undo record).
  - **Destructive / external** actions (delete files, make a purchase, post
    publicly): hard gate, always requires explicit typed confirmation, never
    batched with other actions.
- **Auto-heal sentry** (Brahma's other good idea): a background watcher
  catches agent runtime exceptions, uses the LLM to diagnose and propose a
  patch, and — for the agent's own skill code only — applies it inside the
  sandbox. Never silently patches the user's own files; those go through the
  normal write-tier confirm.

### 6.8 RAG over the user's own documents
- A local embedding model + SQLite vector store, indexed over the user's
  chosen folders (projects, notes, reference docs).
- Agents can search this index during any turn, so the assistant reasons
  about *the user's* files and context, not just its own memory.
- What gets indexed is user-selected at setup; the index lives on-disk.

### 6.9 Proactive behavior
- Scheduled/continuous agents are already part of OpenJarvis (morning digest,
  monitors). Extend the trigger list beyond schedules to **events**: a build
  fails → investigate and report; inbox hits a threshold → triage; a tracked
  deadline is <48h away and the task is still open → surface it.
- Proactive messages are shown, not assumed: the agent can *raise* an issue,
  but acting on it still follows the permission tiers in §6.7.

### 6.10 An auditor/critic agent
- A second agent, cheaper than the main one, whose only job is to review the
  main agent's *planned* actions against the safety policy in §6.7 before
  execution — not after.
- It can flag, hold, or pass an action; it cannot execute anything itself.
- Cheap to add because OpenJarvis is multi-agent-native; this is what makes
  the autonomy defensible rather than just permissive.

## 7. The GUI — 3D Holo-Flux orb (settled)

- A dedicated desktop app window (Electron or a native Windows WebView2 shell
  wrapping a web canvas) hosting a real 3D orb: perspective projection,
  ~900-particle shell with depth sorting, hex frames rotating in 3D,
  drag-to-orbit, scroll-to-zoom. No WebGL library required — a plain 2D
  canvas with manual 3D projection runs comfortably on CPU at this particle
  count.
- **The orb is driven by the audio pipeline, not by text:**
  - `idle` — slow auto-rotation, blue, faint breathing.
  - `listening` — the user's live mic energy drives a pull-in of the shell
    plus expanding ripple rings; cyan.
  - `thinking` — no audio, so a slow internal pulse; the shell spins faster;
    amber.
  - `speaking` — Kokoro's output energy drives the particle shimmer and a
    bottom spectrum strip; brighter cyan.
  - `muted` — everything dies down to grey.
- **Version 1: dedicated window only.** A floating always-on-top overlay
  that follows across apps (true "heads-up display" JARVIS mode) is an
  explicit **later** extension, not part of v1. This keeps v1's scope to
  "orb you talk to in a window" rather than "orb you can't get rid of
  across every app," and the overlay's screen-space behavior is a genuinely
  separate problem (window manager quirks, focus stealing, multi-monitor)
  not worth coupling into the first build.
- Chat history, execution trace cards (Brahma's "Inspect" drawer idea:
  each agent step shows its latency and raw payload on expand), and a
  settings hub (model choice, cloud escalation policy, permission tiers,
  API keys) live alongside the orb in the same window, on a side or
  bottom panel.

## 8. What the builder decides (left open on purpose)

- Exact desktop-shell technology for the orb window (Electron vs. Tauri vs.
  native WebView2 + a thin native host): pick on install size, update
  story, and how hard "run as a scheduled-task background service" is on
  that stack. All three can host a 2D-canvas 3D orb; this is a plumbing
  call, not a design call.
- Exact 8B model choice for the local LLM (Llama-3-class 8B vs. a newer
  equivalent): pick whichever is best-supported by OpenJarvis's runtime
  today and re-test annually; the architecture doesn't depend on which 8B.
- Which specific document folders get RAG-indexed by default at setup:
  sensible default = the user's active project folder(s), user confirms
  at first run; the exact list is the user's call, not the builder's.

## 9. Phased build order (for the eventual plan, not this doc's scope)

1. **Chassis + voice loop** — OpenJarvis installed, 8B local model via
   Ollama, Kokoro TTS wired in, Whisper + wake word + VAD running a clean
   local loop. A plain terminal/text UI is fine here; the orb is not yet
   required for this phase to be "done."
2. **The orb** — build the 3D Holo-Flux window, wire it to the same
   audio-pipeline state machine from step 1.
3. **The three day-to-day jobs** — morning digest (email/calendar/tasks
   connectors), the code-assistant agent (OpenJarvis's CodeAct agent,
   pointed at the user's project folders), and project/deadline tracking
   (a local store the agent reads and updates, plus the proactive
   deadline-surfacing trigger).
4. **The sophistication layer** — permission tiers + the auditor agent
   first (safety), then the safe self-evolving skill sandbox, then the
   auto-heal sentry, then RAG over the user's docs, then the local
   embedding model and vector store.
5. **Floating overlay mode** — the v2 "true HUD" extension, only after
   v1 is stable on the dedicated window.

## 10. What the user decided, as decided (not builder's call)

- Hardware target: 16–18 GB RAM, integrated graphics, Windows 11 laptop.
- Brain: OpenJarvis (framework), not Brahma's code (license).
- Mouth: Kokoro-82M, local.
- Ears: local Whisper-class STT + wake word + VAD, no cloud.
- Core LLM: 8B @ 4-bit on CPU, cloud escalation for hard tasks.
- Orb: B (particle swarm) + D (holo treatment) hybrid, rebuilt as a true
  3D object (perspective, drag-to-orbit, scroll-to-zoom), driven by the
  audio pipeline.
- GUI: dedicated desktop app window for v1; floating overlay is explicitly
  deferred, not dropped.
- Day-to-day jobs: morning voice briefing, code help, project/deadline
  tracking, plus general voice conversation.
- **Email/calendar/task provider: Google (Gmail/Calendar/Tasks).**
- **Cloud escalation target: Agnes AI** (OpenAI-compatible API at
  `https://apihub.agnes-ai.com/v1`; confirmed model `agnes-3.0-flash`;
  credentials in gitignored `.env`).
- **Self-evolving-skill sandbox: Docker on WSL2, memory-capped (~4 GB)**
  (builder's recommendation, user chose "best one"); fallback to a
  restricted-process sandbox behind the same approval gate.

## 11. Open until implementation (flagged, not blocking)

- Whether WSL2 + Docker coexist comfortably with the 8B local model in
  the ~12 GB RAM budget on the user's specific laptop (verify during
  Phase 1; the ~4 GB memory cap is the control).

## 12. Resolved during planning

- Agnes AI escalation: base URL `https://apihub.agnes-ai.com/v1`,
  model `agnes-3.0-flash`, verified with a live chat-completions call on
  2026-11-06.
- Whether WSL2 + Docker actually coexist comfortably with the 8B local model
  in the ~12 GB RAM budget on the user's specific laptop (verify during
  Phase 4; the memory cap is the control).
