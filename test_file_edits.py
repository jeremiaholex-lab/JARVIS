"""Stage 3 file edits: propose a diff, apply only on the typed phrase.

Offline — real files on tmp_path, a deterministic-only auditor, no network.
The invariants pinned here:
  * propose() never writes a byte to disk;
  * a rejection and a missing/wrong phrase leave the file untouched;
  * only the exact typed phrase applies, and the executor (not the UI) checks it;
  * a stale propose is refused rather than clobbering a newer write;
  * credential-shaped paths, VCS dirs, binaries and multi-match replaces are refused.
"""
import json
import tempfile
from pathlib import Path

import pytest

from jarvis.agent import tools as agent_tools
from jarvis.security.auditor import Auditor
from jarvis.security.file_edit import CONFIRM_PHRASE, EDIT_TOOL, SecuredFileEdits
from jarvis.security.tiers import PlannedAction, Tier, classify


@pytest.fixture
def editor():
    return SecuredFileEdits(Auditor(use_llm=False))


@pytest.fixture
def target(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    return p


# ------------------------------------------------------------------ tier + tool

def test_file_edit_is_destructive_tier():
    assert classify(PlannedAction(tool=EDIT_TOOL, params={})) is Tier.DESTRUCTIVE


def test_file_edit_tool_is_offered_as_destructive():
    tool = next(t for t in agent_tools.build_tools() if t.name == EDIT_TOOL)
    assert tool.tier is Tier.DESTRUCTIVE
    assert EDIT_TOOL in {s["function"]["name"] for s in agent_tools.tool_schemas(agent_tools.build_tools())}


def test_unwired_editor_reports_a_clean_error(monkeypatch):
    agent_tools.configure_file_edits(None)
    out = json.loads(agent_tools.execute(agent_tools.build_tools(), EDIT_TOOL,
                                         {"action": "replace", "path": "/x", "old_string": "a",
                                          "new_string": "b"}))
    assert "not wired up" in out["error"]


# --------------------------------------------------------------- propose only

def test_propose_does_not_touch_the_file(editor, target):
    before = target.read_text()
    r = editor.propose({"action": "replace", "path": str(target),
                        "old_string": "beta", "new_string": "BETA"})
    assert r["pending"] is True and r["token"]
    assert r["tier"] == "destructive" and r["requires_phrase"] is True
    assert "+BETA" in r["diff"] and "-beta" in r["diff"]
    assert target.read_text() == before


def test_propose_through_the_tool_layer_leaves_disk_alone(editor, target):
    agent_tools.configure_file_edits(editor)
    try:
        out = json.loads(agent_tools.execute(
            agent_tools.build_tools(), EDIT_TOOL,
            {"action": "replace", "path": str(target), "old_string": "alpha",
             "new_string": "ALPHA"}))
    finally:
        agent_tools.configure_file_edits(None)
    assert out["pending"] is True
    assert target.read_text() == "alpha\nbeta\ngamma\n"


# ---------------------------------------------------------------------- apply

def test_phrase_applies_the_edit(editor, target):
    r = editor.propose({"action": "replace", "path": str(target),
                        "old_string": "beta", "new_string": "BETA"})
    res = editor.resolve(r["token"], True, CONFIRM_PHRASE)
    assert not res.get("error")
    assert res["outcome"]["applied"] is True
    assert target.read_text() == "alpha\nBETA\ngamma\n"


def test_phrase_matching_is_forgiving_on_punctuation_and_case(editor, target):
    for phrase in ("YES DO IT", "Yes, do it!", "  yes,   do it  "):
        r = editor.propose({"action": "replace", "path": str(target),
                            "old_string": "beta", "new_string": "beta2"})
        assert editor.resolve(r["token"], True, phrase)["outcome"]["applied"] is True
        target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")


def test_missing_phrase_does_not_apply_and_keeps_the_token_live(editor, target):
    r = editor.propose({"action": "replace", "path": str(target),
                        "old_string": "beta", "new_string": "BETA"})
    refused = editor.resolve(r["token"], True, "")
    assert "requires_phrase" in refused or "type" in refused["error"]
    assert target.read_text() == "alpha\nbeta\ngamma\n"
    # The token is still pending, so the user can settle it properly.
    assert editor.has(r["token"]) is True
    assert editor.resolve(r["token"], True, CONFIRM_PHRASE)["outcome"]["applied"] is True


def test_wrong_phrase_is_refused(editor, target):
    r = editor.propose({"action": "replace", "path": str(target),
                        "old_string": "beta", "new_string": "BETA"})
    assert "error" in editor.resolve(r["token"], True, "yes")
    assert target.read_text() == "alpha\nbeta\ngamma\n"


def test_reject_leaves_the_file_and_clears_the_token(editor, target):
    r = editor.propose({"action": "replace", "path": str(target),
                        "old_string": "beta", "new_string": "BETA"})
    res = editor.resolve(r["token"], False)
    assert res["outcome"] == {"skipped": True}
    assert target.read_text() == "alpha\nbeta\ngamma\n"
    assert editor.pending() == []


def test_create_makes_a_new_file(editor, tmp_path):
    new = tmp_path / "sub" / "new.txt"
    r = editor.propose({"action": "create", "path": str(new), "content": "hello\n"})
    assert r["pending"] is True
    assert not new.exists()
    assert editor.resolve(r["token"], True, CONFIRM_PHRASE)["outcome"]["applied"] is True
    assert new.read_text() == "hello\n"


def test_rewrite_replaces_the_whole_body(editor, target):
    r = editor.propose({"action": "rewrite", "path": str(target), "content": "only\n"})
    editor.resolve(r["token"], True, CONFIRM_PHRASE)
    assert target.read_text() == "only\n"


# --------------------------------------------------------------------- guards

def test_secret_shaped_path_is_refused_at_propose(editor, tmp_path):
    r = editor.propose({"action": "create", "path": str(tmp_path / ".env"), "content": "x=1\n"})
    assert r["pending"] is False and "credential" in r["error"]


def test_vcs_dir_is_refused(editor, tmp_path):
    r = editor.propose({"action": "create", "path": str(tmp_path / ".git" / "config"),
                        "content": "x\n"})
    assert r["pending"] is False and "refusing" in r["error"]


def test_stale_propose_is_refused(editor, target):
    r = editor.propose({"action": "replace", "path": str(target),
                        "old_string": "beta", "new_string": "BETA"})
    target.write_text("someone else wrote this\n", encoding="utf-8")
    res = editor.resolve(r["token"], True, CONFIRM_PHRASE)
    assert "changed since" in res["error"]
    assert target.read_text() == "someone else wrote this\n"


def test_ambiguous_old_string_is_refused(editor, tmp_path):
    p = tmp_path / "dup.txt"
    p.write_text("x\nx\n", encoding="utf-8")
    r = editor.propose({"action": "replace", "path": str(p), "old_string": "x", "new_string": "y"})
    assert r["pending"] is False and "2 times" in r["error"]


def test_missing_old_string_is_refused(editor, target):
    r = editor.propose({"action": "replace", "path": str(target),
                        "old_string": "not here", "new_string": "y"})
    assert r["pending"] is False and "does not appear" in r["error"]


def test_binary_file_is_refused(editor, tmp_path):
    p = tmp_path / "img.bin"
    p.write_bytes(b"\x00\x01\x02binary")
    r = editor.propose({"action": "rewrite", "path": str(p), "content": "text\n"})
    assert r["pending"] is False and "binary" in r["error"]


def test_relative_path_is_refused(editor):
    r = editor.propose({"action": "create", "path": "notes.txt", "content": "x\n"})
    assert r["pending"] is False and "absolute" in r["error"]


def test_create_never_overwrites(editor, target):
    r = editor.propose({"action": "create", "path": str(target), "content": "clobber\n"})
    assert r["pending"] is False and "already exists" in r["error"]


# ----------------------------------------------------------------------- undo

def test_undo_restores_previous_contents(editor, target):
    original = target.read_text()
    r = editor.propose({"action": "replace", "path": str(target),
                        "old_string": "beta", "new_string": "BETA"})
    editor.resolve(r["token"], True, CONFIRM_PHRASE)
    assert target.read_text() != original
    assert editor.undo(r["token"])["undone"] is True
    assert target.read_text() == original


def test_double_resolve_is_refused(editor, target):
    r = editor.propose({"action": "replace", "path": str(target),
                        "old_string": "beta", "new_string": "BETA"})
    editor.resolve(r["token"], True, CONFIRM_PHRASE)
    assert "error" in editor.resolve(r["token"], True, CONFIRM_PHRASE)


def test_shouted_confirmation_still_settles(editor, target):
    """The confirmation patterns carry re.IGNORECASE, which also uppercases the
    token they capture, so a shouted phrase arrives as FE_AB12. The executor
    must not report that as an unknown token.

    Named without the word "token": pytest builds tmp_path from this function's
    name, and is_sensitive() substring-matches the whole path — a directory
    called ..._token_... would be refused as a credential file before the
    behaviour under test ever ran.
    """
    r = editor.propose({"action": "replace", "path": str(target),
                        "old_string": "beta", "new_string": "BETA"})
    assert editor.resolve(r["token"].upper(), True, "YES, DO IT")["outcome"]["applied"] is True
    assert target.read_text() == "alpha\nBETA\ngamma\n"


def test_shouted_confirmation_can_be_undone(editor, target):
    original = target.read_text()
    r = editor.propose({"action": "replace", "path": str(target),
                        "old_string": "beta", "new_string": "BETA"})
    editor.resolve(r["token"], True, CONFIRM_PHRASE)
    assert editor.undo(r["token"].upper())["undone"] is True
    assert target.read_text() == original


def test_minted_tokens_are_always_lowercase():
    """The orb's card-button path (confirm_edit) forwards the token verbatim,
    so it settles only because ``propose()`` mints lowercase. That safety is
    incidental, not asserted anywhere else — pin it, so a future change to the
    id format cannot silently break the button while the typed-phrase path
    (which now lowercases) keeps passing.
    """
    import re

    from jarvis.security.file_edit import SecuredFileEdits
    from jarvis.security.auditor import Auditor

    editor = SecuredFileEdits(Auditor(use_llm=False))
    with tempfile.TemporaryDirectory() as d:
        note = Path(d) / "note.txt"
        note.write_text("a\nb\n", encoding="utf-8")
        for _ in range(50):
            token = editor.propose({"action": "replace", "path": str(note),
                                    "old_string": "b", "new_string": "c"})["token"]
            assert re.fullmatch(r"fe_[0-9a-f]{16}", token), token


def test_shouted_edit_token_never_reaches_the_write_resolver():
    """Executor normalisation is not enough: Telegram routes on the raw token.

    ``token.startswith("fe_")`` is case-sensitive, so an uppercased edit token
    fell through to resolve_write — a different executor, which then reported
    "unknown write token" for an edit that was plainly pending. Drives the real
    handle_message rather than a copy of its branches.
    """
    import jarvis.channels.telegram as tg

    class FakeBrain:
        def __init__(self):
            self.edits, self.writes = [], []

        def resolve_edit(self, token, approved, phrase=""):
            self.edits.append((token, approved, phrase))
            return {"token": token, "approved": approved, "outcome": {"applied": approved}}

        def resolve_write(self, token, approved):
            self.writes.append((token, approved))
            return {"token": token, "approved": approved, "outcome": {}}

        def pending_writes(self):
            return []

        def pending_edits(self):
            return []

    bot = tg.TelegramBot(token="test", brain=FakeBrain())
    bot.send = lambda chat_id, text: None
    bot.api = lambda method, **params: {"result": []}

    for text in ("YES, DO IT FE_CD34", "CANCEL FE_CD34"):
        bot.handle_message(1, text)
    # A bare yes is answered with the phrase hint and never reaches a resolver.
    bot.handle_message(1, "YES FE_CD34")

    assert bot.brain.writes == [], "an edit token must never reach the write resolver"
    assert [t for t, _, _ in bot.brain.edits] == ["fe_cd34", "fe_cd34"]
    assert [a for _, a, _ in bot.brain.edits] == [True, False]
    # The phrase survives routing intact — the executor still checks it.
    assert bot.brain.edits[0][2] and "do it" in bot.brain.edits[0][2].lower()

    # And a write token still goes to the write surface, lowered.
    bot.handle_message(1, "YES PW_AB12")
    assert bot.brain.writes == [("pw_ab12", True)]


# -------------------------------------------------- confirmation grammar (hosts)
#
# These import the real grammar from file_edit rather than re-declaring a copy.
# A local copy proves only that the copy matches; that was exactly how the two
# hosts drifted apart (Telegram captured any token, the orb only fe_).

def test_edit_confirm_grammar_requires_all_three_words():
    from jarvis.security.file_edit import CONFIRM_PHRASE, EDIT_CONFIRM_RE

    token = "fe_abc123"
    assert EDIT_CONFIRM_RE.match(f"{CONFIRM_PHRASE} {token}")
    assert EDIT_CONFIRM_RE.match(f"yes do it {token}")
    assert EDIT_CONFIRM_RE.match(f"YES, DO IT {token}")
    assert not EDIT_CONFIRM_RE.match(f"yes {token}")
    assert not EDIT_CONFIRM_RE.match("do it")
    assert not EDIT_CONFIRM_RE.match("yes, do it")  # no token
    assert EDIT_CONFIRM_RE.match(f"{CONFIRM_PHRASE} {token}").group(2) == token


def test_edit_confirm_grammar_rejects_a_write_token():
    """The token is constrained to fe_: a phrase aimed at a write token must
    fall through to the misplaced-phrase hint, not the edit resolver."""
    from jarvis.security.file_edit import (
        CONFIRM_PHRASE,
        EDIT_CONFIRM_RE,
        EDIT_PHRASE_MISPLACED_RE,
    )

    write_token = "pw_ab12"
    assert not EDIT_CONFIRM_RE.match(f"{CONFIRM_PHRASE} {write_token}")
    m = EDIT_PHRASE_MISPLACED_RE.match(f"{CONFIRM_PHRASE} {write_token}")
    assert m and m.group(2) == write_token


def test_edit_cancel_grammar():
    from jarvis.security.file_edit import EDIT_CANCEL_RE

    assert EDIT_CANCEL_RE.match("no fe_abc123").group(2) == "fe_abc123"
    assert EDIT_CANCEL_RE.match("cancel fe_abc123")
    assert not EDIT_CANCEL_RE.match("no")


def test_write_grammar_accepts_yes_no_and_cancel():
    from jarvis.security.file_edit import WRITE_CONFIRM_RE

    for word in ("yes", "no", "cancel"):
        assert WRITE_CONFIRM_RE.match(f"{word} pw_ab12").group(2) == "pw_ab12"
    assert not WRITE_CONFIRM_RE.match("yes")


def test_bare_yes_never_confirms_an_edit():
    """'yes fe_x' must not reach the model: it cannot apply anything, but it
    would say it did. It is answered with the phrase hint instead, and must
    never match the confirm pattern."""
    from jarvis.security.file_edit import (
        CONFIRM_PHRASE,
        EDIT_BARE_YES_RE,
        EDIT_CONFIRM_RE,
    )

    token = "fe_abc123"
    m = EDIT_BARE_YES_RE.match(f"yes {token}")
    assert m and m.group(1) == token
    assert not EDIT_CONFIRM_RE.match(f"yes {token}"), "a bare yes must never confirm"
    hint = (f"{m.group(1)} is a destructive file edit, so 'yes' alone will not "
            f"apply it. Type '{CONFIRM_PHRASE} {m.group(1)}' to confirm it, "
            f"or 'no {m.group(1)}' to cancel.")
    assert CONFIRM_PHRASE in hint and token in hint


def test_misplaced_phrase_is_handled_offline_in_both_hosts():
    """'yes, do it <not-an-edit-token>' must not become a turn in either host:
    the model cannot apply an edit, so it could only narrate one it never made.
    Both hosts must answer it with the same hint."""
    from jarvis.security.file_edit import (
        CONFIRM_PHRASE,
        EDIT_CONFIRM_RE,
        EDIT_PHRASE_MISPLACED_RE,
    )

    write_token = "pw_ab12"
    text = f"{CONFIRM_PHRASE} {write_token}"
    # It must not be taken for an edit confirmation...
    assert not EDIT_CONFIRM_RE.match(text)
    # ...but it must be catchable, so a host can answer instead of running a turn.
    m = EDIT_PHRASE_MISPLACED_RE.match(text)
    assert m and m.group(2) == write_token


def test_both_hosts_share_the_one_grammar():
    """Defining the grammar in file_edit is the whole point: both hosts import
    the same objects, so they cannot disagree about a confirmation."""
    from jarvis.security import file_edit
    import jarvis.channels.telegram as tg
    import orb.server as orb

    assert tg._EDIT_CONFIRM_RE is file_edit.EDIT_CONFIRM_RE
    assert tg._CONFIRM_RE is file_edit.WRITE_CONFIRM_RE
    assert orb._EDIT_CONFIRM_RE is file_edit.EDIT_CONFIRM_RE
    # Both hosts must be able to catch a misplaced phrase, or it runs a turn.
    assert tg._EDIT_PHRASE_MISPLACED_RE is file_edit.EDIT_PHRASE_MISPLACED_RE
    assert orb._EDIT_PHRASE_MISPLACED_RE is file_edit.EDIT_PHRASE_MISPLACED_RE


def test_wrong_phrase_from_chat_box_leaves_the_token_settleable(editor, target):
    """Mirrors the orb: a mismatched phrase is refused but the user can retry."""
    r = editor.propose({"action": "replace", "path": str(target),
                        "old_string": "beta", "new_string": "BETA"})
    refused = editor.resolve(r["token"], True, "yes")
    assert "error" in refused
    assert target.read_text() == "alpha\nbeta\ngamma\n"
    assert editor.has(r["token"]) is True
    done = editor.resolve(r["token"], True, CONFIRM_PHRASE)
    assert done["outcome"]["applied"] is True
    assert target.read_text() == "alpha\nBETA\ngamma\n"
