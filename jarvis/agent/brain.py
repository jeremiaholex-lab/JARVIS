"""The JARVIS text brain: Ollama + Agnes engines, read-only tools, one turn.

Extracted so a non-voice host (the Telegram bot, a script, the console) can
run the *same* agent turn the orb does, without loading the voice stack
(Whisper / Kokoro / openWakeWord / Silero). The orb keeps its own copy of the
voice plumbing; this module is only the text path: engines + the read-tier
tools + ``run_turn``.

Bootstrap note: the vendored OpenJarvis checkout must be on ``sys.path``
before ``openjarvis.*`` imports resolve, and the runtime-state home must be
pinned to ``JARVIS/.openjarvis`` first, or RAG indexes and audit state land
in ``~/.openjarvis``. Mirrors ``orb/server.py``.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import List, Optional, Sequence

_logger = logging.getLogger(__name__)

JARVIS = Path(__file__).resolve().parents[2]


def _bootstrap_paths() -> None:
    """Make the vendored runtime and the JARVIS root importable, then pin the
    runtime-state home. Idempotent — safe to run from any host process."""
    candidates = (
        os.environ.get("OPENJARVIS_SRC"),
        os.environ.get("OPENJARVIS_HOME"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "OpenJarvis", "src"),
    )
    src_root = next(
        (Path(c) for c in candidates if c and (Path(c) / "src" / "openjarvis").is_dir()),
        JARVIS / "openjarvis_runtime",
    )
    for p in (str(src_root / "src"), str(JARVIS)):
        if p not in sys.path:
            sys.path.insert(0, p)
    os.environ.setdefault("OPENJARVIS_HOME", str(JARVIS / ".openjarvis"))


# --- Settings: the same shape the orb uses, so the brain routes identically ---

def default_settings() -> dict:
    cfg = {}
    if (JARVIS / ".env").exists():
        for line in (JARVIS / ".env").read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return {
        "local_model": "qwen3:1.7b",
        "cloud": {
            "base_url": cfg.get("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1"),
            "model": "agnes-3.0-flash",
        },
        "vision": {"cloud_consent": False},
    }


def load_settings() -> dict:
    s = default_settings()
    saved = JARVIS / "orb" / "settings.json"
    if saved.exists():
        try:
            data = json.loads(saved.read_text())
            for k, v in data.items():
                if isinstance(v, dict) and isinstance(s.get(k), dict):
                    s[k].update(v)
                else:
                    s[k] = v
        except Exception:
            _logger.debug("settings.json unreadable, using defaults")
    return s


SYSTEM_PROMPT = (
    "Your assistant name and identity is JARVIS, a personal AI assistant. "
    "This identity is separate from the underlying AI model or provider. If asked "
    "your name, say JARVIS. You were created by Jeremiah Olan. "
    "Answer concisely, in one to three plain sentences unless asked to elaborate. "
    "No markdown, no bullet lists — replies may be read aloud or sent to a chat."
)


class Brain:
    """One shared agent turn driver: engines built once, tools read-only.

    Thread-safe for sequential turns (each host uses one Brain). The local
    engine is guarded by a lock exactly as the orb's is, so concurrent turns
    never interleave into one Ollama call.
    """

    def __init__(self, settings: Optional[dict] = None):
        _bootstrap_paths()
        self.settings = settings or load_settings()
        self._env = self._read_env()
        self._cloud_key = self._env.get("AGNES_API_KEY", "")

        from jarvis.agent.tools import build_tools, tool_schemas, configure_vision

        self.tools = build_tools()
        self.schemas = tool_schemas(self.tools)
        # No screen on a chat host: wire a vision context that reports absent,
        # so look_at_screen degrades to a clean error instead of a crash.
        configure_vision(lambda: (None, None, self.settings))

        self._local = None
        self._cloud = None
        self._gen_lock = threading.Lock()
        self._init_local_engine()

    @staticmethod
    def _read_env() -> dict:
        cfg = {}
        if (JARVIS / ".env").exists():
            for line in (JARVIS / ".env").read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
        return cfg

    def _init_local_engine(self) -> None:
        try:
            from openjarvis.engine.ollama import OllamaEngine

            self._local = OllamaEngine()
        except Exception as exc:
            _logger.warning("local engine unavailable: %s", exc)

    @property
    def cloud_available(self) -> bool:
        return bool(self._cloud_key)

    def _cloud_engine(self):
        if self._cloud is None:
            from openjarvis.engine.openai_compat_engines import OpenAICompatEngine

            base = self.settings["cloud"]["base_url"].rstrip("/").removesuffix("/v1")
            self._cloud = OpenAICompatEngine(host=base, api_key=self._cloud_key or None)
        return self._cloud

    def call_engine(self, engine: str, msgs: List[dict], tools: Optional[List[dict]]) -> dict:
        """Adapt plain dicts to the framework's Message/ToolCall shapes."""
        from openjarvis.core.types import Message, Role, ToolCall

        role_map = {r.value: r for r in Role}
        converted = []
        for m in msgs:
            calls = [
                ToolCall(id=c.get("id", ""), name=c.get("name", ""),
                         arguments=c.get("arguments", "{}"))
                for c in (m.get("tool_calls") or [])
            ]
            converted.append(Message(
                role=role_map.get(m.get("role", "user"), Role.USER),
                content=m.get("content") or "",
                tool_calls=calls or None,
                tool_call_id=m.get("tool_call_id"),
            ))
        kwargs: dict = {"temperature": 0.3}
        kwargs["max_tokens"] = 400 if engine == "cloud" else 220
        if tools:
            kwargs["tools"] = tools
        if engine == "cloud":
            return self._cloud_engine().generate(converted, model=self.settings["cloud"]["model"], **kwargs)
        if self._local is None:
            raise RuntimeError("local engine is not available")
        with self._gen_lock:
            return self._local.generate(converted, model=self.settings["local_model"], num_ctx=4096, **kwargs)

    def run(self, text: str, memory: Sequence = (), force_cloud: bool = False):
        """One agent turn: route -> read-tier tools -> answer. Returns a TurnResult."""
        from jarvis.agent.turn import run_turn

        return run_turn(
            text,
            system_prompt=SYSTEM_PROMPT,
            memory=memory,
            tools=self.schemas,
            tool_impls=self.tools,
            call_engine=self.call_engine,
            local_model=self.settings["local_model"],
            cloud_model=self.settings["cloud"]["model"],
            cloud_available=self.cloud_available,
            force_cloud=force_cloud,
        )


__all__ = ["Brain", "load_settings", "SYSTEM_PROMPT"]
