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
- Make microphone controls and their active states discoverable; do not imply microphone access when unavailable.
- Display model preference separately from the actual model used for a turn. Slash commands change preference; the backend remains responsible for fallback.
- Keep advanced settings and read-only diagnostics out of the primary workspace but one click away.
- Preserve the voice-responsive orb, desktop HUD, and existing safety gates.

## Voice output

Speech is a separate concern from the answer: the full reply appears the moment it exists, and audio follows as it is generated. Controls live in Advanced — voice source (automatic/hosted/local), pacing (quick start or gapless), spoken length, and speech rate — because they are trade-offs, not defaults to discover by accident. The assistant states plainly when microphone capture or the hosted voice is unavailable, and never leaves a reply silently half-spoken: a failed piece is retried, the rest still plays, and the text remains on screen.
