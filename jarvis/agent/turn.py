"""One JARVIS conversation turn: route, tools, answer, fallback (design §6.4).

Framework-free on purpose. The caller injects ``call_engine(engine, messages,
tools)``, which returns an OpenAI-shaped dict (`content`, optional
`tool_calls`). That keeps this module unit-testable with fake engines and lets
the orb own the plumbing to Ollama / Agnes.

A turn can:
  * consult read-tier tools for as many rounds as it needs (bounded),
  * escalate to the cloud when the local answer fails the confidence check,
  * report the engine and the routing reason so the UI can show them.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from .routing import (LOW_CONFIDENCE, Route, _ascii_text, needs_retry,
                      route_turn, tool_nudge_needed)

_logger = logging.getLogger(__name__)

TOOL_GUIDANCE = (
    "You have read-only tools for the user's own data: their projects and "
    "deadlines, their indexed documents, read-only Google mail/calendar/task data, "
    "their Notion notes, a read-only web search, and read-only access to files on "
    "their computer (read_file, list_dir, search_files). Use a tool "
    "and the current time. Use a tool "
    "whenever the answer depends on that data instead of guessing. Never "
    "invent a fact about the user's projects, files, or schedule. When you "
    "need data, call the tool now — do not say you will search, and do not "
    "claim you cannot find something without having called a tool. "
    "You may also propose one write to the user's project/deadline store with "
    "jarvis_projects_write, but it only QUEUES the change — it returns a "
    "confirmation token and nothing is saved until the user confirms it in the "
    "UI. Never claim a write happened; say it is waiting for their confirmation."
)


@dataclass(slots=True)
class TurnResult:
    answer: str
    engine: str
    model: str
    route_reason: str
    steps: List[dict] = field(default_factory=list)
    tools_used: List[str] = field(default_factory=list)


EngineCall = Callable[[str, List[Dict], Optional[List[Dict]]], Dict]


def _messages(system_prompt: str, memory: Sequence, text: str) -> List[Dict]:
    """Build the prompt, dropping the assistant's own past failures.

    Live finding: replaying "I couldn't find that in your documents" taught
    the next turn to say the same thing instead of calling a tool — the memory
    was teaching it to fail. Weak answers stay in the archive for inspection,
    they just do not go into the prompt.
    """
    msgs: List[Dict] = [{"role": "system", "content": system_prompt}]
    for m in memory:
        role = m["role"] if isinstance(m, dict) else m.role
        content = m["content"] if isinstance(m, dict) else m.content
        if not content:
            continue
        if role == "assistant" and LOW_CONFIDENCE.match(_ascii_text(content)):
            continue
        msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": text})
    return msgs


def _run_engine_pass(
    *,
    engine: str,
    model: str,
    msgs: List[Dict],
    tools: List[Dict],
    tool_impls: List,
    call_engine: EngineCall,
    max_rounds: int,
    max_tokens: int,
    trace: List[dict],
    tools_used: List[str],
) -> str:
    """Drive the tool loop on one engine; return the final text answer."""
    working = list(msgs)
    answer = ""
    for _round in range(max(1, max_rounds)):
        t0 = time.time()
        res = call_engine(engine, working, tools or None)
        ms = round((time.time() - t0) * 1000)
        calls = res.get("tool_calls") or []
        if not calls:
            answer = str(res.get("content", "")).strip()
            trace.append({
                "name": "llm", "engine": f"{engine} ({model})", "ms": ms,
                "reply": answer, "usage": res.get("usage", {}),
            })
            return answer
        working.append({"role": "assistant", "content": res.get("content") or "", "tool_calls": calls})
        for call in calls:
            name = call.get("name", "")
            args = call.get("arguments", "{}")
            from . import tools as agent_tools

            payload = agent_tools.execute(tool_impls, name, args)
            tools_used.append(name)
            trace.append({
                "name": "tool", "tool": name, "ms": 0,
                "arguments": args if isinstance(args, str) else json.dumps(args),
                "result": payload[:400],
            })
            working.append({
                "role": "tool",
                "tool_call_id": call.get("id", "") or f"call_{name}",
                "content": payload,
            })
    trace.append({"name": "llm", "engine": f"{engine} ({model})", "ms": 0, "reply": "(tool limit reached)"})
    return "I could not finish that in one go — please narrow it down."


def run_turn(
    text: str,
    *,
    system_prompt: str,
    memory: Sequence = (),
    tools: Sequence = (),
    tool_impls: Sequence = (),
    call_engine: EngineCall,
    local_model: str,
    cloud_model: str,
    cloud_available: bool = True,
    force_cloud: bool = False,
    local_tokens_per_second: Optional[float] = None,
    max_tool_rounds: int = 4,
) -> TurnResult:
    route = route_turn(
        text,
        force_cloud=force_cloud,
        cloud_available=cloud_available,
        local_tokens_per_second=local_tokens_per_second,
    )
    tools = list(tools)
    tool_impls = list(tool_impls)
    msgs = _messages(f"{system_prompt}\n\n{TOOL_GUIDANCE}" if tools else system_prompt, memory, text)
    trace: List[dict] = []
    tools_used: List[str] = []

    first = route.engine
    model = cloud_model if first == "cloud" else local_model
    try:
        answer = _run_engine_pass(
            engine=first, model=model, msgs=msgs, tools=tools, tool_impls=tool_impls,
            call_engine=call_engine, max_rounds=max_tool_rounds, max_tokens=400,
            trace=trace, tools_used=tools_used,
        )
    except Exception as exc:
        if first != "local" or not cloud_available:
            raise
        route = Route("cloud", f"local engine failed ({exc.__class__.__name__})")
        answer = _run_engine_pass(
            engine="cloud", model=cloud_model, msgs=msgs, tools=tools, tool_impls=tool_impls,
            call_engine=call_engine, max_rounds=max_tool_rounds, max_tokens=400,
            trace=trace, tools_used=tools_used,
        )
        return TurnResult(answer, "cloud (fallback)", cloud_model, route.reason, trace, tools_used)

    # A data/screen question answered without ever calling a tool is usually
    # the model guessing at its own limits, or promising to act. Nudge once.
    if tools and tool_nudge_needed(answer, used_tools=bool(tools_used)):
        nudged = msgs + [{
            "role": "system",
            "content": "Do not answer from memory. Call the tool now to read the "
                       "user's actual data, then answer from what it returns.",
        }]
        try:
            retry = _run_engine_pass(
                engine=first, model=model, msgs=nudged, tools=tools, tool_impls=tool_impls,
                call_engine=call_engine, max_rounds=max_tool_rounds, max_tokens=400,
                trace=trace, tools_used=tools_used,
            )
            if retry and not LOW_CONFIDENCE.match(_ascii_text(retry)):
                answer = retry
        except Exception as exc:
            _logger.warning("tool nudge failed: %s", exc)

    # §6.4 confidence check: a weak local answer gets one cloud retry.
    if (
        first == "local"
        and cloud_available
        and needs_retry(answer, words_in_question=len(text.split()),
                        used_tools=bool(tools_used))
    ):
        retry_route = Route("cloud", "local answer failed the confidence check")
        try:
            answer = _run_engine_pass(
                engine="cloud", model=cloud_model, msgs=msgs, tools=tools, tool_impls=tool_impls,
                call_engine=call_engine, max_rounds=max_tool_rounds, max_tokens=400,
                trace=trace, tools_used=tools_used,
            )
            return TurnResult(answer, "cloud (escalated)", cloud_model, retry_route.reason, trace, tools_used)
        except Exception as exc:
            _logger.warning("cloud confidence retry failed: %s", exc)

    label = f"{first} ({model})"
    return TurnResult(answer, label, model, route.reason, trace, tools_used)


__all__ = ["TurnResult", "run_turn", "TOOL_GUIDANCE"]
