---
name: JARVIS — Signal Workspace
description: "A voice-first assistant workspace with a living orb, a persistent conversation, and focused secondary controls."
colors:
  background: "#090d15"
  surface: "#101724"
  surface-raised: "#141d2b"
  text: "#f2f6ff"
  text-secondary: "#abb9cd"
  text-muted: "#77879e"
  accent: "#709dff"
  success: "#78dab2"
typography:
  body: "Manrope, Segoe UI, system-ui, sans-serif"
  technical: "JetBrains Mono, Cascadia Code, Consolas, monospace"
---

# JARVIS — Signal Workspace

## Direction

A calm, modern control room for an assistant: a responsive particle orb makes the assistant's state tangible, while a persistent conversation and direct composer make its capabilities immediately usable. The interface is atmospheric without becoming a decorative dashboard. State, connection, and actual model preference should be real data, not invented telemetry.

## Layout

On wide screens, the voice stage and conversation sit side by side. The stage holds the state-reactive orb, a clear status, the listening and microphone-mute controls, and a model selector. The conversation is always visible, with the transcript above a fixed composer. The empty state introduces the slash shortcuts. On narrow screens, the stage stacks above the conversation; very short landscape windows scroll rather than clipping controls. Advanced settings and diagnostics live in an independently scrollable drawer; Escape and the Conversation button close it. The floating HUD remains a separate compact mode.

## Visual language

Use near-black blue surfaces, a quiet blue accent, cool white type, subtle borders, and generous space. The orb transitions colour to communicate listening, thinking, speaking, and muted states. Use Manrope for interface and conversation, and mono type sparingly for status and small labels. Rounded panels and minimal shadows provide depth without obscuring content. Preserve readable text, keyboard focus, accessible control names, and reduced-motion preferences.

## Interaction principles

- Chat and confirmations must remain visible and usable; don't hide approvals in settings.
- Desktop control is propose-only at the tool boundary: a card is shown before any OS input, and typed `yes, do it` plus exact snapshot equality is the only key that unlocks execution.
- Confirm one desktop action per request; reject multi-step chains and mixed batches instead of executing them partially.
- Stop controls are the visible HUD Stop and `Ctrl+Alt+J`; a new user turn also cancels pending work. Do not intercept Escape globally.
- Hide sensitive windows (password managers, banking, private/incognito browsing) from facts, cards, target lists, and screenshot crops; a sensitive window vetoes a crop. Sensitive-sounding local document paths remain exact as approved payloads but are redacted in evidence previews.
- Keep the safe launch policy strict: documents, folders, common applications, and shortcuts are allowed; executables outside common applications, scripts, URLs, network paths, and alternate streams are rejected before and again before execution.
- Warn prominently on likely password/secret entry, but allow explicit approval after the warning.
- Make microphone controls and their active states discoverable; do not imply microphone access when unavailable.
- Display model preference separately from the actual model used for a turn. Slash commands change preference; the backend remains responsible for fallback.
- Keep advanced settings and read-only diagnostics out of the primary workspace but one click away.
- Preserve the voice-responsive orb, desktop HUD, and existing safety gates.

## Desktop control flow

The agent can propose seven non-READ desktop tools: open app/file, move cursor, click, scroll, type, and press keys. Calling one registers a `DesktopAction` and returns a confirmation payload; it performs no OS input. The card shows only evidence previews (a local crop and safe descriptions), never the executable target; sensitive window/paths are redacted in that preview while the approved target stays exact. Approval consumes the exact snapshot once; a different snapshot, a wrong phrase, a spent card, or a stale card fails closed. The native overlay heartbeat is a hard gate: no live shell means no card and no input. Execution revalidates the launch policy and stop state immediately before reaching the injected Win32 boundary. A new turn, HUD Stop, or `Ctrl+Alt+J` settles the pending action without executing it. Password-like text is flagged on the card but remains approvable by explicit typed confirmation.

## Voice output

Speech is a separate concern from the answer: the full reply appears the moment it exists, and audio follows as it is generated. Controls live in Advanced — voice source (automatic/hosted/local), pacing (quick start or gapless), spoken length, and speech rate — because they are trade-offs, not defaults to discover by accident. The assistant states plainly when microphone capture or the hosted voice is unavailable, and never leaves a reply silently half-spoken: a failed piece is retried, the rest still plays, and the text remains on screen.
