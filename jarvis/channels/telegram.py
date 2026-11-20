"""JARVIS on Telegram: one private bot, the full agent brain, zero new deps.

Long-polls the raw Bot API with ``requests`` (python-telegram-bot is not a
project dependency, and the runtime's own Telegram adapter falls back to
send-only when it is missing). Inbound text goes through the same
:class:`~jarvis.agent.brain.Brain` the orb runs — routing, read-tier tools
(web search, Notion, Google, projects), escalation — so a chat message gets
the same answers a voice question would.

Writes the brain proposes (jarvis_projects_write) never run on their own.
After each turn the bot lists the pending writes (token, summary, auditor
reason) and the user settles each one by replying ``yes <token>`` to confirm
or ``no <token>`` to cancel; the decision goes through the same host
confirmation surface (Brain.resolve_write) the orb UI uses, and lands in the
shared audit chain.

File edits (jarvis_file_edit) are destructive, so they take a typed phrase
instead of a tap: the bot posts the diff, and the user replies
``yes, do it <token>`` to apply it — the phrase is checked by the executor,
not by this file, so a typo simply leaves the edit pending.

Setup (owner, one time):
  1. Talk to @BotFather on Telegram, /newbot, copy the token.
  2. Add TELEGRAM_BOT_TOKEN=123456:ABC... to .env
  3. Optionally JARVIS_TELEGRAM_ALLOWED_CHATS=your_chat_id to keep it private.
  4. Run:  python connect_telegram.py
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

_logger = logging.getLogger(__name__)

JARVIS = Path(__file__).resolve().parents[2]
_API = "https://api.telegram.org"
_MAX_LEN = 4096
_HISTORY_TURNS = 5  # user+assistant pairs kept per chat

# Confirmation grammar is defined once in jarvis.security.file_edit so this host
# and the orb cannot drift (they did: Telegram captured any token where the orb
# captured only fe_, so "yes, do it pw_x" was routed to the edit resolver).
from jarvis.security.file_edit import (
    EDIT_CONFIRM_RE,
    EDIT_PHRASE_MISPLACED_RE,
    WRITE_CONFIRM_RE,
)

# "yes pw_abc123" / "no pw_abc123" / "cancel pw_abc123" — settle a write. A
# fe_ token reaching this branch is handled by the startswith("fe_") case below,
# so cancel and a bare yes for an edit are both settled here, not by separate
# patterns.
_CONFIRM_RE = WRITE_CONFIRM_RE
# "yes, do it fe_abc..." — a file edit is destructive and needs the phrase.
_EDIT_CONFIRM_RE = EDIT_CONFIRM_RE
# A phrase aimed at anything else: answer offline rather than let the model
# claim it applied something.
_EDIT_PHRASE_MISPLACED_RE = EDIT_PHRASE_MISPLACED_RE
_EDIT_PHRASE_HINT = (
    "{token} is a destructive file edit, so 'yes' alone will not apply it. "
    "Reply 'yes, do it {token}' to confirm it, or 'no {token}' to cancel."
)

_HELP = (
    "I'm JARVIS. Send me a question and I answer with my usual tools "
    "(projects, your documents, Google, Notion, web search).\n"
    "Commands: /reset clears this chat's memory with me; /cloud <question> "
    "forces the stronger cloud model for one answer.\n"
    "Writes I propose wait for you: I list each one with a token, then "
    "reply yes <token> to confirm it or no <token> to cancel it.\n"
    "File edits are destructive: I post the diff, and only "
    "'yes, do it <token>' applies one."
)


def _seed_env() -> None:
    """Mirror orb/server.py: pull bot config keys from .env into os.environ."""
    if not (JARVIS / ".env").exists():
        return
    for line in (JARVIS / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k in ("TELEGRAM_BOT_TOKEN", "JARVIS_TELEGRAM_ALLOWED_CHATS"):
                os.environ.setdefault(k, v)


class TelegramBot:
    """One Bot API session: poll loop + an ordered worker that runs the brain.

    Messages are processed strictly in receive order (one worker, a queue),
    so two quick questions never race the local engine or the history file.
    """

    def __init__(
        self,
        token: str = "",
        brain=None,
        history_path: Optional[Path] = None,
        allowed_chat_ids: str = "",
    ):
        _seed_env()
        self.token = (token or os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
        if not self.token:
            raise RuntimeError(
                "no Telegram bot token. Add TELEGRAM_BOT_TOKEN to .env "
                "(create one with @BotFather, /newbot)."
            )
        self.allowed = {c.strip() for c in allowed_chat_ids or os.environ.get("JARVIS_TELEGRAM_ALLOWED_CHATS", "").split(",") if c.strip()}
        self.brain = brain
        self.history_path = Path(history_path or JARVIS / ".telegram-history.json")
        self._history: Dict[str, List[dict]] = self._load_history()
        self._offset = 0

    # -- history --------------------------------------------------------------

    def _load_history(self) -> Dict[str, List[dict]]:
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
            return {k: v for k, v in data.items() if isinstance(v, list)}
        except FileNotFoundError:
            return {}
        except Exception:
            _logger.warning("telegram history unreadable, starting fresh")
            return {}

    def _save_history(self) -> None:
        try:
            self.history_path.write_text(json.dumps(self._history), encoding="utf-8")
        except Exception:
            _logger.warning("could not persist telegram history", exc_info=True)

    # -- Bot API --------------------------------------------------------------

    def api(self, method: str, **params) -> dict:
        import requests

        response = requests.post(f"{_API}/bot{self.token}/{method}", json=params, timeout=35)
        response.raise_for_status()
        return response.json()

    def send(self, chat_id, text: str) -> None:
        text = text or "(empty answer)"
        for i in range(0, len(text), _MAX_LEN):
            self.api("sendMessage", chat_id=chat_id, text=text[i:i + _MAX_LEN])

    # -- message handling -----------------------------------------------------

    def handle_message(self, chat_id, text: str) -> Optional[str]:
        """One inbound message; returns the reply text (None = no reply).

        Pure-ish: no polling, no threading — so it is directly testable.
        Side effects: history file + one sendMessage per reply chunk.
        """
        chat_id = str(chat_id)
        if self.allowed and chat_id not in self.allowed:
            self.send(chat_id, "This bot is private. Ask the owner to add your chat id to JARVIS_TELEGRAM_ALLOWED_CHATS.")
            return None

        text = text.strip()
        if text == "/reset":
            # Store an empty list (not a pop): this chat is explicitly
            # cleared, so the next message must NOT re-seed from shared
            # episodes — /reset means "start blank." A chat that was never
            # used (no key at all) is a different case: it seeds, so
            # cross-channel conversations carry over.
            self._history[chat_id] = []
            self._save_history()
            self.send(chat_id, "Memory for this chat cleared.")
            return None
        if text == "/help" or text.startswith("/start"):
            self.send(chat_id, _HELP)
            return None

        force_cloud = False
        if text.startswith("/cloud"):
            force_cloud = True
            text = text[len("/cloud"):].strip()
            if not text:
                self.send(chat_id, "Usage: /cloud <question>")
                return None

        if self.brain is None:
            self.brain = _lazy_brain()

        # Tokens are minted lowercase, but re.IGNORECASE applies to the capture
        # group too, so a shouted confirmation hands us FE_X. Normalise at the
        # point the token enters routing: the startswith("fe_") check below and
        # the executor lookups are both case-sensitive, and the executor's own
        # normalisation cannot help a token that was routed to the wrong surface.
        confirm = _EDIT_CONFIRM_RE.match(text)
        if confirm:
            return self._resolve_edit_reply(chat_id, True, confirm.group(2).lower(),
                                            confirm.group(1))
        misplaced = _EDIT_PHRASE_MISPLACED_RE.match(text)
        if misplaced:
            # "yes, do it pw_x" — the phrase is for edits, the token is not an
            # edit. Answer here; a turn would let the model claim it applied.
            self.send(chat_id, f"{misplaced.group(2)} is not a file edit token. "
                               "Edits are confirmed with 'yes, do it fe_…'.")
            return None
        confirm = _CONFIRM_RE.match(text)
        if confirm:
            approved = confirm.group(1).lower() == "yes"
            token = confirm.group(2).lower()
            if token.startswith("fe_"):
                if approved:
                    # "yes fe_x" is not enough for a destructive edit: leave it
                    # pending and say the phrase is required.
                    self.send(chat_id, _EDIT_PHRASE_HINT.format(token=token))
                    return None
                return self._resolve_edit_reply(chat_id, False, token, "")
            return self._resolve_confirm_reply(chat_id, approved, token)

        try:
            self.api("sendChatAction", chat_id=chat_id, action="typing")
        except Exception:
            pass  # cosmetic only

        memory = self._history.get(chat_id, [])
        if chat_id not in self._history:
            # First time this chat is used here: pull the recent few shared
            # episodes so a conversation started on another channel (the orb)
            # continues instead of starting from zero. Capped to the last few
            # turns; the full transcript stays in the shared store.
            seed = []
            episodic = getattr(getattr(self.brain, "memory", None), "recent_turns", None)
            if callable(episodic):
                try:
                    seed = [{"role": t.role, "content": t.content[:300]}
                            for t in episodic(limit=_HISTORY_TURNS)]
                except Exception:
                    _logger.warning("seeding chat history from shared memory failed")
            memory = seed
        try:
            result = self.brain.run(text, memory=memory, force_cloud=force_cloud)
        except Exception as exc:
            _logger.exception("telegram turn failed")
            self.send(chat_id, f"I hit a problem answering that ({exc.__class__.__name__}).")
            return None

        reply = str(getattr(result, "answer", "")).strip()
        if not reply:
            reply = "(I didn't get an answer.)"
        self._history[chat_id] = (memory + [{"role": "user", "content": text},
                                            {"role": "assistant", "content": reply}])[-2 * _HISTORY_TURNS:]
        self._save_history()
        self.send(chat_id, reply)
        self._announce_pending_writes(chat_id)
        self._announce_pending_edits(chat_id)
        return reply

    # -- write confirmation ---------------------------------------------------

    def _announce_pending_writes(self, chat_id: str) -> None:
        """After a brain turn, list the writes awaiting confirmation, if any.

        The brain stays the source of truth (SecuredWrites.pending via
        Brain.pending_writes); this host only offers its own yes/no reply.
        """
        pending = getattr(self.brain, "pending_writes", None)
        if not callable(pending):
            return
        try:
            items = pending() or []
        except Exception:
            _logger.exception("listing pending writes failed")
            return
        if not items:
            return
        lines = [f"{len(items)} write{'s' if len(items) > 1 else ''} are waiting on your confirmation. "
                 "Reply yes <token> to confirm, no <token> to cancel:"]
        for pw in items:
            lines.append(f"- {pw.get('token', '?')}: {pw.get('summary', '?')} "
                         f"(auditor {pw.get('auditor', '?')}: {pw.get('reason', '')})")
        self.send(chat_id, "\n".join(lines))

    def _resolve_confirm_reply(self, chat_id: str, approved: bool, token: str) -> None:
        """Settle one queued write through the shared host-confirmation surface."""
        resolve = getattr(self.brain, "resolve_write", None)
        if not callable(resolve):
            self.send(chat_id, "This bot is not wired to confirm writes.")
            return None
        try:
            result = resolve(token, approved)
        except Exception as exc:
            _logger.exception("write confirmation failed")
            self.send(chat_id, f"Confirming that write failed ({exc.__class__.__name__}).")
            return None
        outcome = result.get("outcome", {}) if isinstance(result, dict) else {}
        if isinstance(result, dict) and result.get("error"):
            msg = f"I could not resolve {token}: {result['error']}"
        elif not approved:
            msg = f"Cancelled {token} — nothing was written."
        elif isinstance(outcome, dict) and outcome.get("error"):
            msg = f"Confirmed {token}, but the write failed: {outcome['error']}"
        else:
            msg = f"Confirmed {token} — the write is done."
        self.send(chat_id, msg)
        return None

    def _announce_pending_edits(self, chat_id: str) -> None:
        """Post the diff of each queued file edit and the phrase that applies it."""
        pending = getattr(self.brain, "pending_edits", None)
        if not callable(pending):
            return
        try:
            items = pending() or []
        except Exception:
            _logger.exception("listing pending edits failed")
            return
        for pe in items:
            token = pe.get("token", "?")
            head = (f"Proposed edit {token} — {pe.get('summary', '?')}\n"
                    "Nothing has changed yet.")
            diff = pe.get("diff") or "(no diff)"
            if pe.get("diff_truncated"):
                diff += "\n… diff truncated"
            tail = (f"\nReply 'yes, do it {token}' to apply it, "
                    f"or 'no {token}' to cancel.")
            self.send(chat_id, f"{head}\n\n{diff}\n{tail}")

    def _resolve_edit_reply(self, chat_id: str, approved: bool, token: str,
                            phrase: str = "") -> None:
        """Settle one queued file edit; the executor checks the phrase itself."""
        resolve = getattr(self.brain, "resolve_edit", None)
        if not callable(resolve):
            self.send(chat_id, "This bot is not wired to apply file edits.")
            return None
        try:
            result = resolve(token, approved, phrase)
        except Exception as exc:
            _logger.exception("file edit confirmation failed")
            self.send(chat_id, f"Confirming that edit failed ({exc.__class__.__name__}).")
            return None
        if not isinstance(result, dict):
            self.send(chat_id, f"Unexpected result settling {token}.")
            return None
        if result.get("error"):
            msg = f"I could not apply {token}: {result['error']}"
        elif not approved:
            msg = f"Cancelled {token} — the file is unchanged."
        else:
            oc = result.get("outcome") or {}
            msg = (f"Applied {token} to {oc.get('path', 'the file')} "
                   f"(+{oc.get('added', 0)} -{oc.get('removed', 0)}).")
        self.send(chat_id, msg)
        return None

    # -- poll loop ------------------------------------------------------------

    def _poll_once(self, q: "queue.Queue") -> None:
        """One long getUpdates call; enqueue any messages, advance the offset."""
        import requests

        params = {"offset": self._offset, "timeout": 30, "allowed_updates": ["message"]}
        response = requests.post(f"{_API}/bot{self.token}/getUpdates", json=params, timeout=35)
        if response.status_code == 409:
            # Another poller is using this token (a second bot process).
            _logger.warning("getUpdates 409: another JARVIS bot is already polling this token; waiting 60s")
            time.sleep(60)
            return
        response.raise_for_status()
        for update in response.json().get("result", []):
            self._offset = max(self._offset, update.get("update_id", 0) + 1)
            q.put(update)

    def run(self) -> None:
        """Block: poll forever on the main thread, run turns in one worker."""
        q: "queue.Queue" = queue.Queue()
        worker = threading.Thread(target=self._worker, args=(q,), name="jarvis-telegram", daemon=True)
        worker.start()
        _logger.info("JARVIS on Telegram: polling. /help in the bot lists commands.")
        while True:
            try:
                self._poll_once(q)
            except Exception as exc:
                _logger.warning("poll failed (%s: %s); retrying in 5s", exc.__class__.__name__, exc)
                time.sleep(5)

    def _worker(self, q: "queue.Queue") -> None:
        while True:
            update = q.get()
            message = update.get("message") or update.get("edited_message")
            if not message or not message.get("text"):
                continue
            try:
                self.handle_message(message["chat"]["id"], message["text"])
            except Exception:
                _logger.exception("telegram message handling failed")


def _lazy_brain():
    from jarvis.agent.brain import Brain

    return Brain()


__all__ = ["TelegramBot"]
