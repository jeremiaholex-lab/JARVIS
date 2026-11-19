# JARVIS Codebase Analysis

Date: 2026-11-18
Scope: `jarvis/` agent core, `orb/` desktop shell + server, security tiers,
speech shaping, skills/sentry, and the top-level test suite.
Method: static read of source + pytest run. Read-only; no code changed.

## TL;DR

The architecture is sound and the security posture is the strongest part of the
repo: a three-tier permission model, read-only agent tools that are tier-gated
at build *and* execution time, credentials kept strictly out of git, and a
deliberately local-first routing policy. The weak spots are concentrated in a
small number of places where a documented intent and the actual behavior have
drifted apart. Five `test_speech_shaping` failures and two latent defects
(sentry, routing) all belong to that same class: the code does something
different from what its tests, docstrings, or comments promise.

Ranked defects are at the bottom. Everything above is context.

---

## 1. Architecture

Local-first desktop assistant on a 16–18 GB / iGPU Windows 11 laptop. Layered:

- **Voice loop (all local):** wake word → Silero VAD → Whisper/faster-whisper
  STT → small local LLM → Kokoro-82M TTS. No cloud required for the loop.
- **Hybrid routing** (`jarvis/agent/routing.py`): local Ollama model first;
  escalate to Agnes (`agnes-3.0-flash`, OpenAI-compatible) on explicit
  request, complexity, `NEEDS_USER_DATA`, measured slow local (<4 tok/s), or a
  failed confidence check. Conservative: no cloud key ⇒ everything stays local.
- **Agent turn** (`jarvis/agent/turn.py`): bounded multi-round tool loop
  (`max_tool_rounds`, default 4), memory failure-filtering, single tool nudge,
  one cloud retry when the local answer fails confidence.
- **Read-only tools** (`jarvis/agent/tools.py`): projects/deadlines, RAG,
  Google + Microsoft 365 metadata, clock, screen perception. No write tool in
  the orb by design — writes stay on the console's ConfirmGate path.
- **Security** (`jarvis/security/`): READ free, WRITE single-tap + undo,
  DESTRUCTIVE hard-gated typed confirm. Pre-execution LLM auditor on top.
- **Skills + sentry** (`jarvis/skills/`, `jarvis/sentry/`): agent-proposed
  skills tested in a 4 GB WSL2/Docker sandbox (restricted-process fallback),
  human approval gate before promotion; background watcher drafts auto-heals.
- **Desktop shell** (`orb/`): C# .NET 8 WPF/WinForms + WebView2 host
  (`ShellHost.cs`, `OrbForm.cs`, `OverlayForm.cs`, `HotKeyWindow.cs`) wrapping
  an HTML5 canvas Holo-Flux orb (~900 depth-sorted particles, manual 3D
  projection, no WebGL). FastAPI + WebSocket server on `127.0.0.1:8765`
  (`orb/server.py`, ~1300 lines) drives audio streaming, VAD state, TTS chunk
  synthesis, and the proactive deadline loop.

This matches `docs/jarvis-agent-design.md` well. The design doc's "settled"
decisions (OpenJarvis chassis, Kokoro mouth, local ears, 8B@Q4 brain + Agnes
escalation, Docker-on-WSL2 sandbox) are all faithfully reflected in code.

---

## 2. What is done right

- **Tier gating is real, not cosmetic.** `tools.py:_check_tier` (168-175)
  classifies each tool and refuses to expose it if it is not `Tier.READ`;
  `execute()` (274-275) re-classifies at call time. The Settings panel and the
  orb run the *same* policy object.
- **Memory filtering prevents learned failure.** `turn.py:_messages`
  (50-68) drops past assistant turns matching `LOW_CONFIDENCE` so the model
  stops replaying "I couldn't find that" — a genuinely thoughtful fix, and it
  documents the live incident that caused it.
- **Conservative default.** `routing.py:route_turn` (126-127) routes local-only
  when no cloud key is configured instead of failing the turn.
- **Secrets hygiene.** `.gitignore` covers `.env`, the OAuth client JSON,
  `.google-token.json`, `.microsoft-token.bin`, `jarvis_projects.db`,
  `models/`, `*.gguf`. No credential path is committed.

---

## 3. Ranked defects

### [High] Speech contract diverged — 5 test failures, of which 2 mask a real bug

`jarvis/speech.py:42` — `split_sentences` was changed to "speak **every**
piece without dropping text" (see its docstring and the module note: Kokoro is
2–3× slower than realtime, so one long synthesis call = long silence; you must
speak everything, sentence by sentence). The orb calls it with
`max_sentences=None` and speaks all pieces.

`test_speech_shaping.py` still encodes the **old** contract, where
`split_sentences` returned a *bounded summary*:

- `test_split_caps_sentences_as_well_as_characters` (37) — expects ≤3 pieces;
  the new impl returns one piece per sentence (~12). Stale.
- `test_briefing_would_say_the_opening_only` (56) — expects total ≤ limit+40;
  new impl returns the whole digest. Stale.
- `test_a_huge_input_still_produces_speakable_output` (71) — expects total ≤200;
  new impl returns all ~24.8k chars. Stale.

But two of the five catch a **real gap** in the new implementation:

- `test_unpunctuated_text_is_clamped_not_passed_through` (76) and
  `test_first_sentence_is_kept_even_when_later_ones_are_dropped` (85) expect
  each emitted piece to be ≤ ~160 chars even for a spaceless 5000-char run or
  a 400-char word. The new `split_sentences` only clamps on **spaces**
  (`speech.py:57-67`), so a single word/run longer than `limit` is emitted
  whole — `[5000]` in the failure output. That defeats the module's own goal
  ("each synthesis call stays reasonably short").

Root cause in one line: **the implementation was migrated to "speak everything"
but kept its word-boundary clamping, so oversized pieces leak through when
there is no space, and the test suite was not migrated with it.** The fix
needs a character-level clamp on individual pieces *and* a decision on which
contract the three "summary" tests should encode. Until then the voice path
can hand Kokoro an arbitrarily long string in exactly the case (a pasted dump,
no punctuation) that the tests were written to guard against.

### [Medium] Sentry never applies its own patch

`jarvis/sentry/watcher.py:heal_failure` (132-158): the code copies the skill
to a scratch tree, writes the LLM's proposed fix to `fix.json`, and runs the
proposed `test_skill.py` against the scratch tree — **but it never actually
applies the fix text / `changed_files` to the copied skill.** So
`sandbox_passed` reflects the *original, still-broken* skill, not the proposed
repair. The docstring says "build a **patched** scratch copy" and the comment
at line 132 says "Patch the skill's code in the scratch copy only," but no
patching step exists. The approval gate is therefore gating on a
meaningless sandbox signal. Consequence: a bad fix can be promoted just as
easily as a good one; the human reviewer is the only real check.

### [Medium] `NEEDS_USER_DATA` bypasses local even though local can answer it

`routing.py:34-43,130-131`: any question matching "my projects / deadlines /
documents / screen …" is escalated to the cloud with reason *"needs your own
data, which the local model could not fetch."* But the local model *can* fetch
that data now — the read-only tools (`jarvis_projects_read`, `jarvis_rag_search`,
`google_read`, `look_at_screen`) exist precisely for it, and `turn.py:168-184`
adds a tool-nudge that fixes the local model's failure mode on data questions.
So the routing rule and the tool-nudge partially contradict each other: you pay
cloud latency + API quota for exactly the class of question the local+tools
path is built to handle. The code comment (30-33) shows this was a deliberate
workaround for a weak local model, but that workaround is now superseded. Worth
either narrowing the rule to "no local read tool applicable" or removing it.

### [Low] `max_tokens` accepted but dropped

`turn.py:80,153` — `run_turn` passes `max_tokens=400` into
`_run_engine_pass`, which declares it as a parameter but **never forwards it to
`call_engine`** (96-98, 117-118). Token bounding therefore depends entirely on
each engine's default, and the 400 you think you're enforcing is a no-op.
Either thread it through to the engine call or delete the parameter.

### [Low] Auditor docstring contradicts the code; permissive default on LLM garbage

`auditor.py:9-11` claims the LLM layer "escalate[s] … when the deterministic
layer **flags** something," but the code (97-98) only invokes the LLM when the
deterministic layer **passes** cleanly. The docstring is wrong; the behavior is
the valuable one (the LLM can only downgrade a clean PASS). Separately,
`_first_enum` (192-199) defaults ambiguous/empty LLM output to `"pass"` —
permissive for a safety critic. Low risk because the deterministic layer has
already run, but for a component whose job is safety, "garbage ⇒ pass" is the
least defensible default.

### [Low] Test collection noise from the vendored runtime

Running pytest over the whole tree fails at **collection** (exit code 2, not
assertion failures) because the vendored `openjarvis_runtime/` tests import
`respx`, which is not installed (`openjarvis_runtime/tests/engine/test_*.py`,
`tests/tools/test_http_request.py`). The project's *own* tests are clean:
`pytest test_*.py orb/test_*.py` → 166 collected, **159 passed, 5 failed
(the speech ones), 2 skipped**. There are also unregistered pytest marks
(`asyncio`, `live`, `cloud`, `apple`, `macos15`) producing warnings — register
them in a `pytest.ini`/`conftest` `markers` list or the runtime tests will keep
warning.

---

## 4. Recommendations, in order

1. **Pick the speech contract and reconcile.** Most likely: keep "speak every
   piece," add a character-level clamp to `split_sentences` so no piece exceeds
   `limit` even without spaces, and rewrite the three stale "summary" tests to
   the new contract. This unblocks 5 red tests *and* closes the oversized-piece
   bug that would otherwise stall Kokoro on unpunctuated input.
2. **Actually apply the sentry patch** in the scratch tree before running the
   sandbox test (or drop the `sandbox_passed` signal and rely on the human
   gate, saying so explicitly in the docstring).
3. **Revisit `NEEDS_USER_DATA`** now that read tools + the tool-nudge exist;
   narrow or remove it so routine "what are my deadlines" stops burning the
   cloud.
4. **Thread `max_tokens` through to `call_engine`** (or remove it).
5. **Fix the auditor docstring** and default `_first_enum` to `HOLD`, not
   `PASS`.
6. **Isolate the vendored runtime's tests** (add `respx` to the dev deps, or
   exclude `openjarvis_runtime/` from the default pytest run) so `pytest` at the
   repo root collects cleanly; register the custom marks.

## 5. Security note (positive)

Nothing in the read of `tools.py`, `routing.py`, `turn.py`, `auditor.py`, or
the connectors surfaced a path where a secret, token, or user file is written
outside the gitignored local boundary. Screen capture is local and consent-gated
before any pixel is sent (`tools.py:137-159`). The DESTRUCTIVE tier is
hard-gated regardless of the auditor (`auditor.py:78-90`). The main security
*code* risk is the permissive LLM-auditor default in defect #5, not a data
leak.
