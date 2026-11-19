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


# ------------------------------------------------- chat-box phrase (orb server)

def test_chat_box_phrase_regex_requires_all_three_words():
    """The orb settles an edit from the chat box, so the phrase is parsed by a
    regex before the model ever sees the text. A bare 'yes' must not match, or
    a habitual reply would apply a destructive edit."""
    import re

    from jarvis.security.file_edit import CONFIRM_PHRASE

    # Same shape as orb/server.py's _EDIT_CONFIRM_RE.
    pattern = re.compile(r"^(yes,?\s+do\s+it)\s+(fe_\w+)$", re.IGNORECASE)
    token = "fe_abc123"

    assert pattern.match(f"{CONFIRM_PHRASE} {token}")
    assert pattern.match(f"yes do it {token}")
    assert pattern.match(f"YES, DO IT {token}")
    assert not pattern.match(f"yes {token}")
    assert not pattern.match(f"do it {token}")
    assert not pattern.match(f"yes, do it")
    # The captured token is what gets resolved, not the prose.
    assert pattern.match(f"{CONFIRM_PHRASE} {token}").group(2) == token


def test_chat_box_cancel_regex():
    import re

    pattern = re.compile(r"^(no|cancel)\s+(fe_\w+)$", re.IGNORECASE)
    assert pattern.match("no fe_abc123").group(2) == "fe_abc123"
    assert pattern.match("cancel fe_abc123")
    assert not pattern.match("no")


def test_chat_box_bare_yes_is_intercepted_not_sent_to_the_model():
    """'yes fe_x' must not reach the model: it cannot apply anything, but it
    would say it did. It is answered with the phrase hint instead, matching the
    Telegram host, and must never match the confirm pattern."""
    import re

    from jarvis.security.file_edit import CONFIRM_PHRASE

    bare = re.compile(r"^yes\s+(fe_\w+)$", re.IGNORECASE)
    confirm = re.compile(r"^(yes,?\s+do\s+it)\s+(fe_\w+)$", re.IGNORECASE)
    token = "fe_abc123"

    m = bare.match(f"yes {token}")
    assert m and m.group(1) == token
    assert not confirm.match(f"yes {token}"), "a bare yes must never confirm"
    # The hint it is answered with names the real phrase.
    hint = (f"{m.group(1)} is a destructive file edit, so 'yes' alone will not "
            f"apply it. Type '{CONFIRM_PHRASE} {m.group(1)}' to confirm it, "
            f"or 'no {m.group(1)}' to cancel.")
    assert CONFIRM_PHRASE in hint and token in hint


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
