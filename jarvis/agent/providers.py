"""Model ladder: Gemini 3.5 -> Gemini 2.5 -> Agnes, with spoken switching.

The owner's instruction (Nov 2026): the Ollama local model was removed for
quality; every turn now goes to a hosted brain, but the two Gemini keys
exist because each key has its own quota - when the 3.5 key is exhausted
the turn falls to 2.5 (second key), and when that is exhausted the turn
falls to Agnes. A rate-limited tier sits in a short cooldown so the next
turn does not rediscover it from scratch, and one spoken sentence flips
the order ("switch to Gemini 2.5").

Design notes:

* Tiers come from settings.json ("providers") over a built-in default, and
  each names the ENV var holding its key. A tier with no key is absent from
  the ladder - the product works on Agnes alone until keys are added.
* `call()` reports WHICH tier answered: the engine label in every trace,
  the UI "brain:" note and the eval suite need the truth, not the wish.
* Fallback triggers on rate limits AND transport/5xx errors; a 4xx that is
  a bad request (auth, schema) is surfaced instead of silently downgrading,
  because downgrading would hide the real bug behind a different brain.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

_logger = logging.getLogger("jarvis.providers")

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


class RateLimited(Exception):
    """429 / RESOURCE_EXHAUSTED: this tier is parked for cooldown_s."""


class ProviderError(Exception):
    """Any other failure for one tier."""


@dataclass
class Tier:
    id: str                    # "gemini35" | "gemini25" | "agnes"
    label: str                 # human/UI name
    kind: str                  # "gemini" | "openai"
    model: str
    key: Optional[str] = None
    base_url: str = ""
    cooldown_until: float = 0.0

    @property
    def available(self) -> bool:
        return bool(self.key)

    @property
    def cooling(self) -> bool:
        return time.time() < self.cooldown_until


DEFAULT_TIERS: List[dict] = [
    {"id": "gemini35", "label": "Gemini 3.5 Flash", "kind": "gemini",
     "model": "gemini-3.5-flash", "key_env": "GEMINI_API_KEY"},
    {"id": "gemini25", "label": "Gemini 2.5 Flash", "kind": "gemini",
     "model": "gemini-2.5-flash", "key_env": "GEMINI_API_KEY_2"},
]


# --------------------------------------------------------------------- adapters
def _gemini_schema(node: dict) -> dict:
    """JSON Schema -> Gemini's OpenAPI-subset schema (types upper-case)."""
    if not isinstance(node, dict):
        return {"type": "STRING"}
    t = node.get("type")
    out: dict = {}
    upper = {"string": "STRING", "integer": "INTEGER", "number": "NUMBER",
             "boolean": "BOOLEAN", "object": "OBJECT", "array": "ARRAY"}.get(str(t).lower())
    if upper:
        out["type"] = upper
    if "enum" in node:
        out["enum"] = node["enum"]
        out.pop("type", None)  # enum alone is valid and avoids type conflicts
    if "description" in node:
        out["description"] = node["description"]
    if "properties" in node:
        out["properties"] = {k: _gemini_schema(v) for k, v in node["properties"].items()}
        if node.get("required"):
            out["required"] = list(node["required"])
    if "items" in node:
        out["items"] = _gemini_schema(node["items"])
    return out or {"type": "STRING"}


def gemini_body(msgs: List[dict], tools: Optional[List[dict]], *,
                max_tokens: int = 400, temperature: float = 0.3) -> dict:
    system_parts: List[str] = []
    contents: List[dict] = []
    for m in msgs:
        role = m.get("role")
        if role == "system":
            if m.get("content"):
                system_parts.append(str(m["content"]))
            continue
        if role == "tool":
            payload = m.get("content") or ""
            try:
                parsed = json.loads(payload)
                response = parsed if isinstance(parsed, dict) else {"result": str(parsed)}
            except Exception:
                response = {"result": str(payload)}
            part = {"functionResponse": {"name": (m.get("tool_call_id") or "tool").removeprefix("call_"),
                                         "response": response}}
            if contents and contents[-1]["role"] == "user" and \
                    all("functionResponse" in p for p in contents[-1]["parts"]):
                contents[-1]["parts"].append(part)  # group parallel results
            else:
                contents.append({"role": "user", "parts": [part]})
            continue
        parts: List[dict] = []
        text = (m.get("content") or "").strip()
        if text:
            parts.append({"text": text})
        for c in m.get("tool_calls") or []:
            args = c.get("arguments") or "{}"
            try:
                args = json.loads(args) if isinstance(args, str) else dict(args)
            except Exception:
                args = {}
            parts.append({"functionCall": {"name": c.get("name", ""), "args": args}})
        if not parts:
            parts = [{"text": "(no content)"}]
        contents.append({"role": "model" if role == "assistant" else "user", "parts": parts})
    body: dict = {"contents": contents,
                  "generationConfig": {"temperature": temperature,
                                       "maxOutputTokens": max_tokens,
                                       "thinkingConfig": {"thinkingBudget": 0}}}
    if system_parts:
        body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
    if tools:
        decls = [{"name": t["function"]["name"],
                  "description": t["function"].get("description", ""),
                  "parameters": _gemini_schema(t["function"].get("parameters") or {})}
                 for t in tools if t.get("function", {}).get("name")]
        if decls:
            body["tools"] = [{"functionDeclarations": decls}]
    return body


def parse_gemini_response(data: dict) -> dict:
    cands = data.get("candidates") or []
    if not cands:
        block = data.get("promptFeedback", {})
        raise ProviderError(f"no candidates (block={block.get('blockReason', 'none')})")
    parts = ((cands[0].get("content") or {}).get("parts")) or []
    text = "".join(p.get("text", "") for p in parts if "text" in p).strip()
    calls = [{"id": p["functionCall"].get("name", ""),
              "name": p["functionCall"].get("name", ""),
              "arguments": json.dumps(p["functionCall"].get("args") or {})}
             for p in parts if "functionCall" in p]
    usage = data.get("usageMetadata") or {}
    return {"content": text,
            "tool_calls": calls or None,
            "usage": {"prompt_tokens": usage.get("promptTokenCount", 0),
                      "completion_tokens": usage.get("candidatesTokenCount", 0)}}


def _openai_clean_msgs(msgs: List[dict]) -> List[dict]:
    """Strict OpenAI bodies: strip harness-only keys ("engine") and expand
    the compact {id,name,arguments} tool-call shape the turn loop echoes
    back into it to the full {type:function,function:{...}} form. The old
    framework engine accepted the compact shape and rebuilt the bodies;
    passing raw dicts to a typed endpoint 400s on messages[N]: missing
    field `type` (found live by the eval's --live pass)."""
    out = []
    for m in msgs:
        clean = {k: v for k, v in m.items()
                 if k in ("role", "content", "tool_calls", "tool_call_id")}
        if m.get("tool_calls"):
            clean["tool_calls"] = [
                {"id": c.get("id") or c.get("name", ""), "type": "function",
                 "function": {"name": c.get("name", ""),
                              "arguments": c.get("arguments", "{}")}}
                for c in m["tool_calls"]]
        if clean.get("content") is None and not clean.get("tool_calls"):
            clean["content"] = ""
        out.append(clean)
    return out


# ------------------------------------------------------------------ the ladder
class ModelLadder:
    """Ordered hosted brains with cooldowns, a spoken preference and a
    post_json transport hook (tests inject a fake; the host uses httpx)."""

    def __init__(self, tiers: List[Tier], *, pref_path: Optional[Path] = None,
                 cooldown_s: float = 120.0,
                 post: Optional[Callable[[str, dict, dict], tuple[int, dict]]] = None) -> None:
        self.tiers = [t for t in tiers]
        self.cooldown_s = float(cooldown_s)
        self.pref_path = Path(pref_path) if pref_path else None
        self._post = post or _httpx_post
        self.preference = self._load_pref()

    # -- preference --------------------------------------------------------
    def _load_pref(self) -> str:
        try:
            if self.pref_path and self.pref_path.exists():
                p = json.loads(self.pref_path.read_text(encoding="utf-8")).get("pref", "auto")
                return p if p in ("auto", "deep") or any(t.id == p for t in self.tiers) else "auto"
        except Exception:
            pass
        return "auto"

    def set_preference(self, pref: str) -> None:
        self.preference = pref
        if self.pref_path is not None:
            try:
                self.pref_path.parent.mkdir(parents=True, exist_ok=True)
                self.pref_path.write_text(json.dumps({"pref": pref}), encoding="utf-8")
            except Exception:
                _logger.warning("model preference could not persist (runtime-only)")

    def tier(self, tid: str) -> Optional[Tier]:
        return next((t for t in self.tiers if t.id == tid), None)

    # -- ordering ----------------------------------------------------------
    def order(self, purpose: str = "standard") -> List[Tier]:
        avail = [t for t in self.tiers if t.available]
        if not avail:
            return []
        if purpose == "deep":
            # "give me the other brain" - agnes first, gemini chain after.
            agnes = [t for t in avail if t.kind == "openai"]
            rest = [t for t in avail if t.kind != "openai"]
            avail = agnes + rest
        if self.preference in ("auto", "deep", ""):
            return avail
        pinned = self.tier(self.preference)
        if pinned is not None and pinned.available:
            # the pinned tier leads; the ladder behind it still saves the turn
            return [pinned] + [t for t in avail if t.id != pinned.id]
        return avail

    # -- calling -----------------------------------------------------------
    def call(self, msgs: List[dict], tools: Optional[List[dict]], *,
             purpose: str = "standard", max_tokens: int = 400,
             timeout_s: float = 120.0) -> tuple[Tier, dict]:
        chain = self.order(purpose)
        if not chain:
            raise ProviderError("no model provider is configured (add GEMINI_API_KEY / "
                                "GEMINI_API_KEY_2 or AGNES_API_KEY to .env)")
        warm = [t for t in chain if not t.cooling]
        tried_any = False
        last_exc: Optional[Exception] = None
        for t in (warm or chain[-1:]):  # all cooling: probe the LAST resort anyway
            tried_any = True
            try:
                resp = self._call_one(t, msgs, tools, max_tokens=max_tokens, timeout_s=timeout_s)
                return t, resp
            except RateLimited as exc:
                t.cooldown_until = time.time() + self.cooldown_s
                _logger.warning("%s rate-limited; parked %.0fs: %s", t.label, self.cooldown_s, exc)
                last_exc = exc
            except ProviderError as exc:
                _logger.warning("%s failed: %s", t.label, exc)
                last_exc = exc
        if not tried_any:
            raise ProviderError("every model tier is cooling down")
        raise last_exc or ProviderError("model ladder exhausted")

    def _call_one(self, t: Tier, msgs: List[dict], tools: Optional[List[dict]], *,
                  max_tokens: int, timeout_s: float) -> dict:
        if t.kind == "gemini":
            url = f"{GEMINI_BASE}/models/{t.model}:generateContent"
            headers = {"x-goog-api-key": t.key or "", "Content-Type": "application/json"}
            body = gemini_body(msgs, tools, max_tokens=max_tokens)
            status, data = self._post(url, headers, body, timeout_s)
            if status == 429:
                raise RateLimited(_brief(data, "rate limited"))
            if status == 400 and "thinking" in json.dumps(data)[:400].lower():
                # some models reject thinkingBudget - retry without it once
                body["generationConfig"].pop("thinkingConfig", None)
                status, data = self._post(url, headers, body, timeout_s)
            if status >= 400:
                raise ProviderError(f"gemini {status}: {_brief(data)}")
            return parse_gemini_response(data)
        # OpenAI-compatible (Agnes)
        url = f"{t.base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {t.key}", "Content-Type": "application/json"}
        body: dict = {"model": t.model, "messages": _openai_clean_msgs(msgs),
                      "max_tokens": max_tokens, "temperature": 0.3}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        status, data = self._post(url, headers, body, timeout_s)
        if status == 429:
            raise RateLimited(_brief(data, "rate limited"))
        if status >= 400:
            raise ProviderError(f"{t.label} {status}: {_brief(data)}")
        msg = ((data.get("choices") or [{}])[0].get("message") or {})
        calls = [{"id": (c.get("function") or {}).get("name", ""),
                  "name": (c.get("function") or {}).get("name", ""),
                  "arguments": (c.get("function") or {}).get("arguments", "{}")}
                 for c in (msg.get("tool_calls") or [])]
        return {"content": (msg.get("content") or "").strip(),
                "tool_calls": calls or None,
                "usage": data.get("usage") or {}}

    # -- introspection ------------------------------------------------------
    def status(self) -> dict:
        return {"preference": self.preference,
                "tiers": [{"id": t.id, "label": t.label, "model": t.model,
                           "available": t.available,
                           "cooling_s": round(max(0.0, t.cooldown_until - time.time()), 1)}
                          for t in self.tiers]}

    def primary_label(self, purpose: str = "standard") -> str:
        chain = self.order(purpose)
        return chain[0].label if chain else "no provider configured"


def _brief(data: dict, default: str = "") -> str:
    try:
        err = data.get("error") or {}
        return str(err.get("message") or err.get("status") or data)[:200] or default
    except Exception:
        return default


def _httpx_post(url: str, headers: dict, body: dict, timeout_s: float) -> tuple[int, dict]:
    import httpx
    try:
        r = httpx.post(url, headers=headers, json=body, timeout=timeout_s)
    except httpx.TimeoutException as exc:
        raise RateLimited(f"timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise ProviderError(f"transport: {exc.__class__.__name__}: {exc}") from exc
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text[:400]}
    return r.status_code, data


# ------------------------------------------------------------ spoken switching
_SWITCH_RE = re.compile(
    r"\b(?:switch|change|move|go|set|make it|use|run|fall back|fallback|stick)\b"
    r"[^.?!]*?\b(?:to|onto|on|into|back to)?\b[^.?!]*?"
    r"(?P<target>(?:gemini[\s-]*)?3[\s.,-]*5|(?:gemini[\s-]*)?2[\s.,-]*5|gemini"
    r"|agnes|the cloud|cloud model|cloud|the api|auto(?:matic)?(?:ly)?|the default|default)"
    r"\b", re.I,
)
_SWITCH_BACK_RE = re.compile(
    r"\b(?:switch|change|go back|move|fall back|use)\b[^.?!]*?\bback\b[^.?!]*?"
    r"(?P<target>gemini[\s-]*3[\s.,-]*5|3\.5|auto(?:matic)?|default)\b", re.I,
)


def parse_model_switch(text: str) -> Optional[str]:
    """Return a tier id or "auto" when the utterance INSTRUCTS a brain change,
    else None. Conservative: it needs a switch verb, and answers with the
    confirmation so a mis-parse is always audible and reversible by voice."""
    t = re.sub(r"\s+", " ", (text or "").strip().lower())
    if not t or len(t.split()) > 14:
        return None
    # An instruction never negates itself: "don't use gemini 2.5" and "stop
    # using agnes" must not flip to the very tier they mention.
    if re.search(r"\b(?:don'?t|do not|never|stop|quit|avoid|not)\b", t):
        return None
    # Questions are not instructions: "did i tell you to switch to gemini 2.5"
    # must not flip the brain; a spoken command starts with the verb.
    if re.match(r"^(?:what|why|how|when|where|who|did|do|does|is|are|should)\b", t):
        return None
    m = _SWITCH_BACK_RE.search(t) or _SWITCH_RE.search(t)
    if not m:
        return None
    target = m.group("target")
    # "switch to gemini" alone is ambiguous between the two keys -> auto
    if re.fullmatch(r"gemini", target.strip()):
        return "auto"
    if "3" in target:
        return "gemini35"
    if "2" in target:
        return "gemini25"
    if re.search(r"agnes|cloud|api", target):
        return "agnes"
    return "auto"


SWITCH_CONFIRM = {
    "gemini35": "Switched. I'll lead with Gemini 3.5 now.",
    "gemini25": "Switched. I'll lead with Gemini 2.5 now.",
    "agnes": "Switched. I'll use Agnes now.",
    "auto": "Back to automatic: Gemini 3.5 first, then 2.5, then Agnes when one runs out.",
}
