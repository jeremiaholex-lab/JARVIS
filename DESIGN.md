---
name: JARVIS — Ambient Orb
description: Dark ambient assistant canvas with a voice-reactive particle orb, state-linked colour, and a compact chat/advanced drawer.
colors:
  bg: "#07080d"
  panel: "rgba(18, 21, 31, .72)"
  panel-raised: "rgba(24, 28, 41, .72)"
  inset: "rgba(5, 6, 10, .6)"
  edge: "rgba(255, 255, 255, .09)"
  edge-quiet: "rgba(255, 255, 255, .05)"
  ink: "#eceff8"
  ink-dim: "#a3abc0"
  ink-faint: "#7a8398"
  accent-default: "#8caaff"
  ok: "#7fd6a0"
  bad: "#ff8a80"
  warn: "#f2c46b"
  orb-state:
    idle: "#8caaff"
    listening: "#46d6c8"
    thinking: "#a87cff"
    speaking: "#ffa468"
    muted: "#686c78"
typography:
  display:
    fontFamily: "Manrope, Segoe UI, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 700
    letterSpacing: ".5em"
  body:
    fontFamily: "Manrope, Segoe UI, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "JetBrains Mono, Cascadia Code, Consolas, monospace"
    fontSize: "10px"
    fontWeight: 500
    letterSpacing: ".1em"
rounded:
  control: "999px"
  panel: "20px"
  card: "10px"
---

# Design System: JARVIS — Ambient Orb

## Overview

**Creative North Star: “The Quiet Signal”**

JARVIS is a calm, dark canvas: the orb is the focal point, not a wall of controls. A deep midnight field and restrained glass surfaces frame a living particle sphere. Colour belongs to the orb's state and flows outward subtly into the UI, so listening, thinking, speaking and muted states are legible without adding dashboard noise. Conversation stays close at hand; diagnostics and settings live in a secondary drawer.

The visual language is atmospheric but restrained: soft ambient light, thin orbital paths, smooth transitions and transparent surfaces. Avoid sci-fi clutter, hard neon, decorative telemetry and heavy control panels. Keep the default screen focused and make advanced information available on demand.

**Key characteristics:**
- The orb is the primary visual and state indicator.
- Each assistant state has a distinct, smoothly transitioning colour.
- The default screen shows the orb, state, latest message and composer.
- Full chat history is one click away; diagnostics and controls are in Advanced.
- Manrope carries human-readable text; JetBrains Mono is reserved for compact technical labels and data.

## Colour

The base is near-black midnight (#07080d) with cool white text. A runtime RGB accent follows the orb's current state:

| State | Accent | Meaning |
| --- | --- | --- |
| Idle | Periwinkle #8caaff | Ready / standby |
| Listening | Teal #46d6c8 | Capturing voice |
| Thinking | Violet #a87cff | Processing |
| Speaking | Orange #ffa468 | Voice output |
| Muted | Slate #686c78 | Microphone muted |

Panels use translucent blue-black surfaces, faint white hairlines and restrained ambient colour. Semantic green, coral and amber are reserved for health, errors and governance verdicts. Keep all text legible against the darkest backgrounds.

## Typography

**Display and body:** Manrope, with Segoe UI/system fallback. **Technical labels:** JetBrains Mono, with Cascadia Code/Consolas fallback.

Manrope keeps the interface contemporary and readable. Mono is limited to connection state, compact labels, metrics and trace payloads. Conversation remains comfortably readable at 14px or larger.

### Hierarchy
- **Wordmark:** 700, 13px, wide tracking, uppercase.
- **State label:** 600, 11px, widely tracked, coloured to the current state.
- **Conversation:** 400, 14–17px / 1.5 line height.
- **Technical label:** 500, 10px, uppercase mono.

## Layout

The default view is a full-window stage: compact header, adaptive centred orb, state label, latest answer and one pill-shaped composer with mic/mute controls. The chat and advanced area slides in as a rounded glass drawer from the upper-right. Chat gets a full-height transcript view; Advanced contains trace, settings, alerts and skills. Escape closes the drawer. On narrow windows, the drawer spans the available width and remains independently scrollable. The floating HUD is a compact overlay variant.

## Elevation and depth

Depth comes from layered translucent surfaces, backdrop blur, thin low-contrast borders and a soft orb-centred ambient field. The orb may glow as a source of state colour; surrounding UI lighting stays subtle. Avoid hard borders, heavy shadows and broad neon halos on controls.

## Shapes

The composer is a pill; mic controls are circles. The drawer uses a 20px radius; inner sections and fields use 10–14px radii. Hairlines are faint and one pixel. Three fine, tilted orbital rings replace the former tick-mark bezel.

## Components

### Composer and controls
- One glass pill contains the prompt input, microphone and mute controls.
- Mic and mute are icon-only in the primary view, with accessible names and tooltips.
- Listening uses the current state accent; mute uses a semantic muted treatment.
- Focus remains clearly visible; keyboard submission uses Enter.

### Chat drawer
- Chat is the default drawer tab and fills the available height with scrollable history.
- Advanced is a separate tab for Trace, Settings, Raised Issues and Skills / Approval.
- Use restrained surface contrast and clear separation between speaker, content and technical metadata.

### Advanced controls and data
- Fields share the translucent inset surface, rounded corners and state-coloured focus ring.
- Trace cards use compact headers and readable wrapped payloads.
- Skill badges use semantic status colours only; status must not rely on colour alone.
- Keep settings labels and explanatory text in body typography, not mono.

### Signature component: the orb
- 1,000-point Fibonacci particle sphere on a responsive 2D canvas; no WebGL or libraries.
- Soft inner light, surface ripple and three tilted, slowly orbiting rings.
- State colours interpolate smoothly: idle periwinkle, listening teal, thinking violet, speaking orange, muted slate.
- Listening emits a pulse; speech animates a circular waveform from audio bins.
- Pointer drag orbits the sphere; scroll zooms.

## Do and don't

### Do
- Encode state in orb colour and label it clearly: STANDBY, LISTENING, PROCESSING, SPEAKING or OFFLINE.
- Preserve a calm, mostly empty stage; put rarely used diagnostics in Advanced.
- Keep inline SVG icons consistent in stroke weight and round caps.
- Keep semantic status understandable through labels as well as colour.
- Render stored user-facing content as text, not injected HTML.

### Don't
- Reintroduce scanlines, chromatic aberration, hex grids or a busy neon dashboard.
- Use mono type for headings or conversational prose.
- Add decorative accent colours unrelated to state or semantic status.
- Let drawer content obscure the primary view when the drawer is closed.
