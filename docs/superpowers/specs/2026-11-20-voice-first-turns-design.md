# Design: Voice-first turns — follow-up window + voice confirmations (roadmap #8)

Date: 2026-11-20
Status: implemented and verified (orb/test_voice_turns.py green)

## Why

Every orb voice turn is a closed cycle: wake word → listen → answer → TTS →
idle. A follow-up question requires saying "Hey JARVIS" again, and a pending
write or file edit can only be settled by typing in the chat box. Both are
text-first habits wearing a voice costume.

## Goals

1. **Follow-up window.** After TTS ends, the orb stays open for the next
   utterance — no wake word needed — for a grace period. Silence times out
   back to idle. Consecutive silent follow-up windows are capped, so a
   quiet room cannot keep the orb listening forever.
2. **Voice confirmations.** A spoken "yes pw_x", "no fe_x", or a bare
   "yes"/"no" (allowed only when exactly one item is pending) settles a
   queued write or file edit offline, mirroring the typed chat path: the
   phrase never reaches the model, and the phrase gate on destructive
   edits is still enforced by the executor.

## Non-goals

- Barge-in (interrupting TTS with speech) — saved for a follow-up.
- Multi-turn wake-free conversation across idle gaps — the window is
  per-reply only, capped at `followup_max_streak` silent rounds.

## Constraints

- `jarvis/security/file_edit.py` owns the confirmation grammar
  (`EDIT_*_RE`, `WRITE_CONFIRM_RE`, `CONFIRM_PHRASE`). The voice path
  matches against the same regexes; only the *input* is normalised
  (`normalize_spoken_confirmation` compacts whitespace, because STT
  transcribes hex tokens with spaces: "yes pw ab12cd…" → "yes pw_ab12cd…").
- The executor (`SecuredFileEdits.resolve`) still requires the phrase for
  DESTRUCTIVE edits, so a spoken "yes fe_x" without the phrase is refused
  the same way a typed one is — the voice layer only routes the words.

## What landed

`orb/server.py`:

- **`Client.followup_streak` / `Client.listen_reason`** — per-connection
  window bookkeeping; `start_listening("follow-up")` is a new reason that
  preserves the streak, while a wake-word or record-button session resets it
  to zero.
- **`playback_done`** — instead of emitting idle, the reply just finished
  playing, so the orb re-arms a follow-up window (muted clients still go
  idle). The 30 s `_speak_watchdog` remains the server-side fallback when
  the UI event never arrives.
- **`maybe_close_utterance`** — the "no segment closed" timeout is now
  window-aware: a follow-up window uses `followup_window_s` (default 8 s)
  and, while `followup_streak < followup_max_streak` (default 2), re-arms
  itself instead of going idle; at the cap it closes the floor. A voiced
  window (VAD close or a voice-confirmation intercept) resets the streak.
- **`resolve_voice_confirmation(text, pending_writes, pending_edits)`** —
  pure, module-level. Normalises STT text, then applies the shared
  `file_edit` regexes in priority order: phrase-confirmed edit > edit
  cancel > misplaced phrase > bare-yes-on-destructive (offline note) >
  write-token match > bare yes/no/cancel (only with exactly one item
  pending). Every match must name a token that is actually pending, so a
  casual "yes please" falls through to the model as a normal turn.
- **Intercept in `maybe_close_utterance`** — after STT, before `do_turn`:
  a resolved verdict is settled through `settle_confirmation` (shared with
  the typed `chat` op, which now routes through the same helper), emits
  the same `write_resolved` / `edit_resolved` + system chat notes, and
  re-opens a follow-up window so the conversation keeps flowing.
- Settings: `followup_window_s` / `followup_max_streak` in
  `default_settings()` and the `settings` op (owner-tunable).

`orb/test_voice_turns.py`:

- 13 pure-logic tests for `normalize_spoken_confirmation` and
  `resolve_voice_confirmation` (gluing, priority, bare-word ambiguity,
  fall-through).
- 5 WebSocket tests through the real handler (stub OWW/VAD/whisper, fake
  agent context): `playback_done` opens the window; muted stays idle;
  silent re-arms up to the streak cap then idle; spoken "yes pw_x" settles
  a pending write offline (executor recorded, model never consulted);
  spoken "no fe_x" cancels a pending edit.

## Calls made (builder's, not agreed upstream)

- `followup_window_s = 8.0` and `followup_max_streak = 2`: an 8 s grace
  feels like a conversational pause, not a loop; two silent re-arms is
  enough for a "wait, actually…" moment and bounds a quiet room. Both are
  owner-tunable via the settings op, so the defaults are cheap to change.
- Bare "cancel"/"nope" are not in the bare-word set — the shared grammar
  only guarantees yes/no/cancel, and "nope" is STT guesswork.
- A voice-settled confirmation re-opens a follow-up window even when the
  verdict was just an offline note (the user mis-spoke and may retry);
  the streak cap still bounds it.
