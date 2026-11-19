"""Describing the screen (design §6.7) — and the consent gate in front of it.

The design classifies "look at the screen" as a read-only action. That is true
of the *capture*: pixels stay on this machine. It is not true of the *reading*,
because this laptop has no local vision model (measured: ~2.5 GB free, and the
only Ollama models installed are text-only). A vision model on this machine
means a cloud model, and sending a screenshot to a cloud model means every
window, token, email preview and filename currently visible leaves the box.

So the rule here is stricter than the tier table:

  * a local vision model, if one is ever installed, is free — same machine;
  * the cloud vision path requires an explicit, recorded consent
    (Settings → vision consent, or JARVIS_VISION_CONSENT=1);
  * without consent nothing is sent, and the answer says exactly how to allow it.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from .screen import Screen

_logger = logging.getLogger(__name__)

# Vision-capable Ollama model families. Matched against `ollama list` output,
# so installing any of these turns screen reading local with no code change.
_LOCAL_VISION_PATTERNS = (
    "llava", "qwen2.5vl", "qwen2-vl", "minicpm-v", "moondream", "bakuvla",
    "gemma3n", "pixtral", "granite-vision",
)

CONSENT_NEEDED = (
    "Screen reading needs a vision model, and this machine has only a cloud one. "
    "A screenshot would leave this computer, so nothing was sent. "
    "Allow it in Settings (vision consent) or by setting JARVIS_VISION_CONSENT=1."
)


@dataclass(slots=True)
class VisionResult:
    text: str
    model: str
    where: str          # "local" | "cloud" | "none"
    ms: int
    reason: str = ""


def local_vision_model(engine) -> Optional[str]:
    """First locally installed vision-capable model, if any."""
    try:
        names = engine.list_models()
    except Exception as exc:
        _logger.debug("list_models failed: %s", exc)
        return None
    for name in names or []:
        lowered = name.casefold()
        if any(p in lowered for p in _LOCAL_VISION_PATTERNS):
            return name
    return None


def consent_given(settings: dict) -> bool:
    if os.environ.get("JARVIS_VISION_CONSENT") == "1":
        return True
    vision = (settings or {}).get("vision") or {}
    return bool(vision.get("cloud_consent"))


def describe_screen(
    screen: Screen,
    question: str,
    *,
    local_engine,
    cloud_factory,
    settings: dict,
) -> VisionResult:
    """Read the current screen. Local model wins; cloud needs consent."""
    started = time.time()
    prompt = (
        "You are looking at the user's screen on their behalf. Answer the "
        "question from what is visible. If the answer is not on screen, say so "
        "in one short sentence. Be specific: quote the exact text, path, or "
        "error message you are reading rather than describing it generally.\n"
        f"Question: {question}"
    )

    model = local_vision_model(local_engine) if local_engine is not None else None
    if model:
        try:
            from openjarvis.core.types import Message, Role

            res = local_engine.generate(
                [Message(role=Role.USER, content=prompt, images=[
                    base64.b64encode(screen.png).decode("ascii")])],
                model=model, max_tokens=400, temperature=0.1,
            )
            text = str(res.get("content", "")).strip()
            if text:
                return VisionResult(text, model, "local", round((time.time() - started) * 1000))
        except Exception as exc:
            _logger.warning("local vision failed (%s: %s), considering cloud",
                            exc.__class__.__name__, exc)

    if not consent_given(settings):
        return VisionResult(CONSENT_NEEDED, "", "none", round((time.time() - started) * 1000),
                            reason="no local vision model and cloud consent not given")

    cloud = settings.get("cloud") or {}
    base = (cloud.get("base_url") or "").rstrip("/").removesuffix("/v1") + "/v1"
    api_key = os.environ.get("AGNES_API_KEY", "")
    if not base or not api_key:
        return VisionResult("Cloud vision is not configured (no base URL or API key).", "", "none",
                            round((time.time() - started) * 1000), reason="cloud not configured")
    try:
        payload = {
            "model": cloud.get("model", "agnes-3.0-flash"),
            "max_tokens": 400,
            "temperature": 0.1,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": screen.data_url}},
                ],
            }],
        }
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(f"{base}/chat/completions", json=payload,
                               headers={"Authorization": f"Bearer {api_key}"})
            resp.raise_for_status()
            data = resp.json()
        text = (data.get("choices", [{}])[0].get("message", {}) or {}).get("content", "")
        text = _clean(str(text).strip())
        if not text:
            return VisionResult("The vision model returned nothing readable.", cloud["model"],
                                "cloud", round((time.time() - started) * 1000))
        return VisionResult(text, cloud.get("model", "?"), "cloud",
                            round((time.time() - started) * 1000))
    except httpx.HTTPStatusError as exc:
        detail = (exc.response.text or "")[:200]
        return VisionResult(f"Cloud vision failed ({exc.response.status_code}): {detail}", "",
                            "cloud", round((time.time() - started) * 1000), reason="http error")
    except Exception as exc:
        return VisionResult(f"Cloud vision failed ({exc.__class__.__name__}: {exc})", "",
                            "cloud", round((time.time() - started) * 1000), reason="transport error")


_VISION_HINT = re.compile(
    r"\b(screen|display|monitor|what (do|can) you see|look at this|on my (screen|desktop))\b",
    re.I,
)


def looks_like_screen_request(text: str) -> bool:
    return bool(_VISION_HINT.search(text or ""))


def _clean(text: str) -> str:
    return " ".join(text.split())


__all__ = [
    "VisionResult", "describe_screen", "local_vision_model", "consent_given",
    "looks_like_screen_request", "CONSENT_NEEDED",
]
