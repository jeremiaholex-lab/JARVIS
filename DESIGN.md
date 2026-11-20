---
name: JARVIS — Ambient Orb
description: "Graphite-and-champagne assistant canvas: a voice-reactive particle orb, state-linked metal accent, serif display type and a compact chat/advanced drawer."
colors:
  bg: "#09090b"
  panel: "rgba(20, 19, 18, .74)"
  panel-raised: "rgba(28, 26, 24, .74)"
  inset: "rgba(6, 6, 7, .62)"
  edge: "rgba(232, 208, 160, .15)"
  edge-quiet: "rgba(232, 208, 160, .08)"
  ink: "#f3eee4"
  ink-dim: "#b9b2a4"
  ink-faint: "#7f796d"
  accent-default: "#d6b476"
  ok: "#8fcfa3"
  bad: "#f09a8e"
  warn: "#e6bf72"
  orb-state:
    idle: "#d6b476"
    listening: "#70c8b8"
    thinking: "#a68ee4"
    speaking: "#e89676"
    muted: "#706c64"
typography:
  display:
    fontFamily: "Cormorant Garamond, Palatino Linotype, Georgia, serif"
    fontSize: "24px"
    fontWeight: 600
    letterSpacing: ".46em"
  body:
    fontFamily: "Manrope, Segoe UI, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  prose:
    fontFamily: "Cormorant Garamond, Palatino Linotype, Georgia, serif"
    fontSize: "17px"
    fontWeight: 500
    lineHeight: 1.5
  label:
    fontFamily: "JetBrains Mono, Cascadia Code, Consolas, monospace"
    fontSize: "10px"
    fontWeight: 500
    letterSpacing: ".1em"
rounded:
  control: "999px"
  panel: "18px"
  card: "14px"
---

# Design System: JARVIS — Ambient Orb

## Overview

**Creative North Star: “The Quiet Signal”**

JARVIS is a calm, dark canvas: the orb is the focal point, not a wall of controls. A warm graphite field, champagne hairlines and restrained glass surfaces frame a living particle sphere. Colour belongs to the orb's state and flows outward subtly into the UI, so listening, thinking, speaking and muted states are legible without adding dashboard noise. Conversation stays close at hand; diagnostics and settings live in a secondary drawer.

The visual language is atmospheric and quietly luxurious: soft ambient light, thin orbital paths, engraved hairline rules, serif display type and transparent surfaces. Avoid sci-fi clutter, hard neon, decorative telemetry and heavy control panels. Keep the default screen focused and make advanced information available on demand.

**Key characteristics:**
- The orb is the primary visual and state indicator.
- Each assistant state has a distinct, smoothly transitioning colour.
- The default screen shows the orb, state, latest message and composer.
- Full chat history is one click away; diagnostics and controls are in Advanced.
- Cormorant Garamond carries the assistant's voice; Manrope handles controls and user text; JetBrains Mono is reserved for compact technical labels and data.

## Colour

The base is near-black graphite (#09090b) with warm white text. A runtime RGB accent follows the orb's current state:

| State | Accent | Meaning |
| --- | --- | --- |
| Idle | Champagne #d6b476 | Ready / standby |
| Listening | Jade #70c8b8 | Capturing voice |
| Thinking | Amethyst #a68ee4 | Processing |
| Speaking | Copper #e89676 | Voice output |
| Muted | Graphite #706c64 | Microphone muted |

Panels use translucent warm-black surfaces, champagne hairlines and restrained ambient colour. Semantic green, coral and amber are reserved for health, errors and governance verdicts. Keep all text legible against the darkest backgrounds.

## Typography

**Display and prose:** Cormorant Garamond (Palatino/Georgia fallback) for the wordmark, section titles, drawer tabs and JARVIS's own answers. **Body and controls:** Manrope, with Segoe UI/system fallback. **Technical labels:** JetBrains Mono, with Cascadia Code/Consolas fallback.

The serif carries the human, classic voice; Manrope keeps controls and user text crisp and contemporary. Mono is limited to connection state, compact labels, metrics and trace payloads. Conversation remains comfortably readable at 14px or larger.

### Hierarchy
- **Wordmark:** 600, 24px, wide tracking, uppercase, flanked by hairline rules.
- **State label:** 600, 11px, widely tracked, coloured to the current state.
- **Assistant prose:** 500, 17px / 1.5 serif. **User text:** 400, 14px / 1.5 sans.
- **Technical label:** 500, 10px, uppercase mono.

## Layout

The default view is a full-window stage: compact header, adaptive centred orb, state label, latest answer and one pill-shaped composer with mic/mute controls. The chat and advanced area slides in as a rounded glass drawer from the upper-right; above 1100px the stage re-centres into the space that remains, and below 880px the drawer becomes a full sheet. Chat gets a full-height transcript view; Advanced contains trace, settings, alerts and skills. Escape closes the drawer. On narrow windows, the drawer spans the available width and remains independently scrollable. The floating HUD is a compact overlay variant.

## Elevation and depth

Depth comes from layered translucent surfaces, backdrop blur, thin champagne borders, a soft orb-centred ambient field and one engraved page frame inset 10px from the window edge. The orb may glow as a source of state colour; surrounding UI lighting stays subtle. Avoid hard borders, heavy shadows and broad neon halos on controls.

## Shapes

The composer is a pill; mic controls are circles. The drawer uses an 18px radius; inner sections use 14px and fields 10px. Hairlines are faint, one pixel and champagne-tinted. Three fine, tilted orbital rings replace the former tick-mark bezel.

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
- State colours interpolate smoothly: idle champagne, listening jade, thinking amethyst, speaking copper, muted graphite.
- Listening emits a pulse; speech animates a circular waveform from audio bins.
- Pointer drag orbits the sphere; scroll zooms.

## Do and don't

### Do
- Keep the accent warm metal, never saturated neon, and let it follow the orb's state.
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
