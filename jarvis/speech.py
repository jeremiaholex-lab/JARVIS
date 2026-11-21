"""Speech shaping shared by the orb and the morning briefing.

Both surfaces had the same latency problem from the same cause: Kokoro on this
CPU-only laptop synthesizes at roughly 2-3x slower than realtime, so however
much text you hand it in one call is exactly how long the user stares at a
silent machine. Speaking sentence by sentence turns one long stall into a
short one, then continuous audio.

This lives in ``jarvis`` rather than ``orb/`` because the briefing worker (no
web server involved) needs it too.
"""
from __future__ import annotations

import re
from typing import List

# Characters spoken before a pause is worth the extra synthesis round trip.
DEFAULT_SPEECH_LIMIT = 150
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?]) +")

# Kokoro's English G2P stalls and mis-aligns on the symbols a terminal-style
# answer is full of: ``/3.5`` produced "words count mismatch" and a 25s chunk
# for ten seconds of audio. Spelling the useful ones out and dropping the
# decorative ones keeps the prosody intact and the synthesis cheap.
_SYMBOL_WORDS = (
    (re.compile(r"\bslash\s*/", re.I), "slash"),
    (re.compile(r"/(?=[a-z0-9])", re.I), " slash "),
    (re.compile(r"[*_`#]"), " "),
    (re.compile(r"\b(\d)[.,](?=\d)"), r"\1 point "),
    (re.compile(r"[|]"), " "),
    (re.compile(r"->|\u2192"), " then "),
    (re.compile(r"\s+"), " "),
)


def spoken_form(text: str) -> str:
    """Rewrite answer text into something the voice can read smoothly."""
    out = " ".join((text or "").split())
    for pattern, repl in _SYMBOL_WORDS:
        out = pattern.sub(repl, out)
    return out.strip()


def speech_text(answer: str, limit: int = DEFAULT_SPEECH_LIMIT) -> str:
    """A short spoken version of a longer written answer.

    The full text stays on screen; the voice gets the first sentences up to
    `limit`, cut at a sentence boundary rather than mid-word.
    """
    text = spoken_form(answer)
    if not text:
        return ""
    if len(text) <= limit:
        return text
    head = text[:limit]
    for stop in (". ", "! ", "? "):
        i = head.rfind(stop)
        if i > limit // 3:
            return head[: i + 1]
    i = head.rfind(" ")
    return (head[:i] if i > limit // 2 else head) + "…"


def split_sentences(text: str, limit: int = DEFAULT_SPEECH_LIMIT,
                   max_sentences: int | None = None) -> List[str]:
    """Split the complete reply into TTS-sized pieces without dropping text.

    Sentence boundaries are preferred. Very long sentences are split on word
    boundaries so each synthesis call stays reasonably short; a single word
    longer than ``limit`` (a pasted run with no spaces) is clamped to its
    first ``limit`` characters plus an ellipsis, so no piece can outgrow one
    synthesis call. ``max_sentences`` remains available for callers that
    explicitly want a summary, but the orb uses the default and speaks every
    piece.
    """
    cleaned = spoken_form(text)
    if not cleaned:
        return []

    pieces: List[str] = []
    for sentence in (p for p in _SENTENCE_SPLIT.split(cleaned) if p):
        words = sentence.split()
        chunk = ""
        for word in words:
            if len(word) > limit:
                # No space inside the word, so the space-based clamp above
                # would hand the whole run to one synthesis call. Speak the
                # first limit characters, marked as cut; the full text stays
                # on screen.
                if chunk:
                    pieces.append(chunk)
                    chunk = ""
                pieces.append(word[:limit] + "…")
                continue
            candidate = f"{chunk} {word}".strip()
            if chunk and len(candidate) > limit:
                pieces.append(chunk)
                chunk = word
            else:
                chunk = candidate
        if chunk:
            pieces.append(chunk)

    if max_sentences is not None:
        pieces = pieces[:max(1, max_sentences)]
    return pieces


__all__ = ["speech_text", "split_sentences", "spoken_form", "DEFAULT_SPEECH_LIMIT"]
