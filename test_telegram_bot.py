"""Telegram bot: offline tests of handle_message with a fake brain and captured API calls."""
import json

import pytest

from jarvis.channels.telegram import TelegramBot


class FakeBrain:
    def __init__(self, answer="hello from JARVIS"):
        self.answer = answer
        self.calls = []

    def run(self, text, memory=(), force_cloud=False):
        self.calls.append({"text": text, "memory": list(memory), "force_cloud": force_cloud})

        class R:
            pass

        r = R()
        r.answer = self.answer
        return r


def make_bot(tmp_path, answer="hello from JARVIS", **kw):
    bot = TelegramBot(
        token="123:fake",
        brain=FakeBrain(answer),
        history_path=tmp_path / "hist.json",
        **kw,
    )
    sent = []
    bot.api = lambda method, **params: sent.append((method, params)) or {"ok": True}
    bot._sent = sent
    return bot


def test_requires_token(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("JARVIS_TELEGRAM_ALLOWED_CHATS", "")
    with pytest.raises(RuntimeError):
        TelegramBot(token="", brain=None, history_path=tmp_path / "h.json")


def test_private_chat_gate_brain_never_runs(tmp_path):
    brain_holder = {"b": None}

    class B(FakeBrain):
        def run(self, *a, **k):
            brain_holder["b"] = True
            return super().run(*a, **k)

    bot = make_bot(tmp_path)
    bot.brain = B()
    bot.allowed = {"owner-id"}
    bot.handle_message("stranger-id", "hi")
    assert brain_holder["b"] is None
    assert any("private" in p.get("text", "") for m, p in bot._sent if m == "sendMessage")


def test_reset_clears_that_chats_history_only(tmp_path):
    bot = make_bot(tmp_path)
    bot._history["c1"] = [{"role": "user", "content": "old"}]
    bot._history["c2"] = [{"role": "user", "content": "keep"}]
    bot.handle_message("c1", "/reset")
    assert "c1" not in bot._history
    assert bot._history["c2"] == [{"role": "user", "content": "keep"}]


def test_help_command_does_not_call_brain(tmp_path):
    bot = make_bot(tmp_path)
    bot.handle_message("c", "/start")
    assert any("JARVIS" in p.get("text", "") for m, p in bot._sent)
    assert bot.brain.calls == []


def test_cloud_prefix_forces_cloud_and_strips_command(tmp_path):
    bot = make_bot(tmp_path)
    bot.handle_message("c", "/cloud what is up")
    call = bot.brain.calls[0]
    assert call["text"] == "what is up"
    assert call["force_cloud"] is True
    assert json.loads((bot.history_path).read_text())["c"][-1] == {"role": "assistant", "content": "hello from JARVIS"}


def test_long_reply_is_chunked_to_telegram_limit(tmp_path):
    bot = make_bot(tmp_path, answer="x" * 9000)
    bot.handle_message("c", "a long one")
    chunks = [p["text"] for m, p in bot._sent if m == "sendMessage"]
    assert len(chunks) == 3
    assert all(len(c) <= 4096 for c in chunks)
    assert "".join(chunks) == "x" * 9000


def test_history_is_bounded_per_chat(tmp_path):
    bot = make_bot(tmp_path)
    for i in range(20):
        bot.handle_message("c", f"q{i}")
    assert len(bot._history["c"]) <= 10
