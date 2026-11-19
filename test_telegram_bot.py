"""Telegram bot: offline tests of handle_message with a fake brain and captured API calls."""
import json

import pytest

from jarvis.channels.telegram import TelegramBot


class FakeBrain:
    def __init__(self, answer="hello from JARVIS", pending=None, write_results=None,
                 pending_edits=None, edit_results=None):
        self.answer = answer
        self.calls = []
        self._pending = pending or []
        self.write_results = write_results or {}
        self.resolves = []
        self._pending_edits = pending_edits or []
        self.edit_results = edit_results or {}
        self.edit_resolves = []

    def run(self, text, memory=(), force_cloud=False):
        self.calls.append({"text": text, "memory": list(memory), "force_cloud": force_cloud})

        class R:
            pass

        r = R()
        r.answer = self.answer
        return r

    def pending_writes(self):
        return list(self._pending)

    def resolve_write(self, token, approved):
        self.resolves.append((token, approved))
        return self.write_results.get((token, approved),
                                      {"token": token, "approved": approved,
                                       "outcome": {"done": True}})

    def pending_edits(self):
        return list(self._pending_edits)

    def resolve_edit(self, token, approved, confirm_phrase=""):
        self.edit_resolves.append((token, approved, confirm_phrase))
        return self.edit_results.get((token, approved),
                                     {"token": token, "approved": approved,
                                      "outcome": {"applied": True, "path": "/tmp/x",
                                                  "added": 1, "removed": 1}})


PENDING = {
    "token": "pw_ab12",
    "action": "jarvis_projects_write",
    "params": {"action": "add_project", "name": "Alpha", "path": "/tmp/alpha"},
    "tier": "write",
    "auditor": "approve",
    "reason": "local project store only",
    "state": "pending",
    "summary": "add project 'Alpha' at '/tmp/alpha'",
}


def _sent_texts(bot):
    return [p.get("text", "") for m, p in bot._sent if m == "sendMessage"]


def make_bot(tmp_path, answer="hello from JARVIS", pending=None, write_results=None, **kw):
    bot = TelegramBot(
        token="123:fake",
        brain=FakeBrain(answer, pending=pending, write_results=write_results,
                        pending_edits=kw.pop("pending_edits", None),
                        edit_results=kw.pop("edit_results", None)),
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


# ------------------------------------------------------------------ writes

def test_pending_writes_are_announced_after_a_turn(tmp_path):
    bot = make_bot(tmp_path, pending=[PENDING])
    bot.handle_message("c", "add a project called Alpha in C:/tmp/alpha")
    texts = _sent_texts(bot)
    announcement = next(t for t in texts if "waiting on your confirmation" in t)
    assert "pw_ab12" in announcement
    assert "add project 'Alpha' at '/tmp/alpha'" in announcement
    assert "yes" in announcement and "no" in announcement
    assert bot.brain.resolves == []


def test_confirm_reply_resolves_with_yes(tmp_path):
    bot = make_bot(tmp_path, write_results={("pw_ab12", True):
                                             {"token": "pw_ab12", "approved": True, "outcome": {"done": True}}})
    bot.handle_message("c", "yes pw_ab12")
    assert bot.brain.resolves == [("pw_ab12", True)]
    assert "Confirmed pw_ab12" in _sent_texts(bot)[0]
    assert bot.brain.calls == []  # the brain never saw the reply


def test_cancel_reply_resolves_with_no(tmp_path):
    bot = make_bot(tmp_path, write_results={("pw_ab12", False):
                                             {"token": "pw_ab12", "approved": False, "outcome": {"skipped": True}}})
    bot.handle_message("c", "no pw_ab12")
    assert bot.brain.resolves == [("pw_ab12", False)]
    assert "Cancelled pw_ab12" in _sent_texts(bot)[0]


def test_confirm_reply_surfaces_a_resolve_error(tmp_path):
    bot = make_bot(tmp_path, write_results={("pw_nope", True): {"error": "unknown write token 'pw_nope'"}})
    bot.handle_message("c", "yes pw_nope")
    assert "unknown write token" in _sent_texts(bot)[0]
    assert bot.brain.resolves == [("pw_nope", True)]


def test_confirm_reply_flags_a_failed_outcome(tmp_path):
    bot = make_bot(tmp_path, write_results={("pw_ab12", True):
                                             {"token": "pw_ab12", "approved": True,
                                              "outcome": {"error": "no such deadline"}}})
    bot.handle_message("c", "YES pw_ab12")  # case-insensitive
    assert bot.brain.resolves == [("pw_ab12", True)]
    assert "the write failed" in _sent_texts(bot)[0]


def test_no_pending_writes_means_no_announcement(tmp_path):
    bot = make_bot(tmp_path)
    bot.handle_message("c", "just a question")
    assert all("waiting on your confirmation" not in t for t in _sent_texts(bot))


def test_bare_yes_is_treated_as_chat_not_a_confirm(tmp_path):
    bot = make_bot(tmp_path, pending=[PENDING])
    bot.handle_message("c", "yes")
    assert bot.brain.calls[0]["text"] == "yes"
    assert bot.brain.resolves == []


# ------------------------------------------------------------------ file edits

PENDING_EDIT = {
    "token": "fe_cd34",
    "action": "jarvis_file_edit",
    "kind": "replace",
    "params": {"action": "replace", "path": "/tmp/notes.txt"},
    "tier": "destructive",
    "auditor": "pass",
    "reason": "destructive: file edits require your typed confirmation",
    "state": "pending",
    "summary": "edit /tmp/notes.txt (+1 -1)",
    "diff": "--- a//tmp/notes.txt\n+++ b//tmp/notes.txt\n@@ -1,3 +1,3 @@\n alpha\n-beta\n+BETA\n gamma\n",
    "diff_truncated": False,
    "requires_phrase": True,
}


def test_pending_edit_is_announced_with_its_diff(tmp_path):
    bot = make_bot(tmp_path, pending_edits=[PENDING_EDIT])
    bot.handle_message("c", "change beta to BETA in notes")
    text = next(t for t in _sent_texts(bot) if "fe_cd34" in t)
    assert "+BETA" in text and "-beta" in text
    assert "yes, do it fe_cd34" in text
    assert "Nothing has changed yet" in text
    assert bot.brain.edit_resolves == []


def test_phrase_reply_applies_the_edit(tmp_path):
    bot = make_bot(tmp_path, pending_edits=[PENDING_EDIT])
    bot.handle_message("c", "yes, do it fe_cd34")
    assert bot.brain.edit_resolves == [("fe_cd34", True, "yes, do it")]
    assert "Applied fe_cd34" in _sent_texts(bot)[0]
    assert bot.brain.calls == []  # the brain never saw the reply


def test_phrase_without_comma_also_works(tmp_path):
    bot = make_bot(tmp_path, pending_edits=[PENDING_EDIT])
    bot.handle_message("c", "yes do it fe_cd34")
    assert bot.brain.edit_resolves == [("fe_cd34", True, "yes do it")]


def test_bare_yes_on_an_edit_token_never_applies(tmp_path):
    bot = make_bot(tmp_path, pending_edits=[PENDING_EDIT])
    bot.handle_message("c", "yes fe_cd34")
    # Nothing is resolved: the edit stays pending and the user is told the phrase.
    assert bot.brain.edit_resolves == []
    assert "yes, do it fe_cd34" in _sent_texts(bot)[0]


def test_edit_cancel_reply(tmp_path):
    bot = make_bot(tmp_path, pending_edits=[PENDING_EDIT])
    bot.handle_message("c", "no fe_cd34")
    assert bot.brain.edit_resolves == [("fe_cd34", False, "")]
    assert "Cancelled fe_cd34" in _sent_texts(bot)[0]


def test_edit_phrase_mismatch_surfaces_the_executor_error(tmp_path):
    bot = make_bot(tmp_path, edit_results={("fe_cd34", True):
                                           {"error": "type 'yes, do it' to confirm it"}})
    bot.handle_message("c", "yes, do it fe_cd34")
    assert "could not apply fe_cd34" in _sent_texts(bot)[0]


def test_write_token_still_resolves_through_the_write_path(tmp_path):
    # A pw_ token must not be routed to the edit resolver.
    bot = make_bot(tmp_path)
    bot.handle_message("c", "yes pw_ab12")
    assert bot.brain.resolves == [("pw_ab12", True)]
    assert bot.brain.edit_resolves == []


def test_phrase_aimed_at_a_write_token_never_reaches_the_edit_resolver(tmp_path):
    # "yes, do it pw_x" must not die on an unknown edit token, and must not
    # reach the model either — it would claim it applied something.
    bot = make_bot(tmp_path)
    bot.handle_message("c", "yes, do it pw_ab12")
    assert bot.brain.edit_resolves == []
    assert bot.brain.resolves == []
    assert bot.brain.calls == []
    assert "not a file edit token" in _sent_texts(bot)[0]


def test_cancel_word_cancels_an_edit(tmp_path):
    bot = make_bot(tmp_path, pending_edits=[PENDING_EDIT])
    bot.handle_message("c", "cancel fe_cd34")
    assert bot.brain.edit_resolves == [("fe_cd34", False, "")]
    assert bot.brain.calls == []


def test_cancel_word_cancels_a_write(tmp_path):
    bot = make_bot(tmp_path)
    bot.handle_message("c", "cancel pw_ab12")
    assert bot.brain.resolves == [("pw_ab12", False)]
    assert bot.brain.edit_resolves == []
    assert bot.brain.calls == []
