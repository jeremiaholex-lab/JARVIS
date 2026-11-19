"""Shared sentence-shaping rules (jarvis.speech).

Both the orb and the morning briefing speak through these, so a long answer
costs the same bounded amount of silence wherever it is spoken.
"""
import pytest

from jarvis.speech import DEFAULT_SPEECH_LIMIT, speech_text, split_sentences


def test_short_answers_survive_untouched():
    assert speech_text("The capital is Paris.") == "The capital is Paris."
    assert split_sentences("The capital is Paris.") == ["The capital is Paris."]


def test_empty_input_speaks_nothing():
    assert speech_text("") == ""
    assert speech_text("   ") == ""
    assert split_sentences("") == []
    assert split_sentences("  \n ") == []


def test_speech_text_breaks_at_a_sentence_boundary_not_midword():
    text = "Alpha bravo charlie. " * 40
    out = speech_text(text, limit=60)
    assert len(out) <= 60
    assert out.endswith(".")
    assert "…" not in out  # a clean sentence boundary was available


def test_speech_text_falls_back_to_a_word_break_then_ellipsis():
    unbroken = "a" * 400
    out = speech_text(unbroken, limit=60)
    assert out == "a" * 60 + "…"


def test_every_sentence_is_spoken_and_bounded():
    long_answer = " ".join(f"Sentence number {i} says something." for i in range(12))
    spoken = split_sentences(long_answer)
    assert len(spoken) == 12
    assert all(len(s) <= DEFAULT_SPEECH_LIMIT + 10 for s in spoken)
    assert " ".join(spoken) == long_answer


def test_every_spoken_piece_ends_on_a_sentence_mark():
    spoken = split_sentences("First idea. Second idea! Third idea? Fourth idea.")
    assert all(s[-1] in ".!?" for s in spoken)
    assert spoken[0] == "First idea."


def test_whitespace_is_collapsed():
    assert split_sentences("  messy \n\n text   here.  second one. ") == [
        "messy text here.", "second one."]


def test_briefing_says_every_section_in_sized_pieces():
    """A real digest shape: every section is spoken, but no single
    synthesis call outgrows the limit."""
    digest = (
        "Good morning. Today is Thursday, November 12. Email: Seeking Alpha: 20% off all products!; "
        "Walmart Canada: NOW LIVE! Up to 70% off furniture. Calendar: 2026-11-12T10:00: standup; "
        "2026-11-12T15:00: design review. Tasks: nothing needing attention. "
        "Projects: JARVIS. No open project deadlines need attention."
    )
    spoken = split_sentences(digest)
    assert len(digest) > 300
    assert all(len(s) <= DEFAULT_SPEECH_LIMIT + 10 for s in spoken)
    assert " ".join(spoken) == digest


def test_a_huge_input_is_spoken_in_bounded_pieces():
    spoken = split_sentences("word " * 5000, limit=150)
    assert len(spoken) > 100
    assert all(len(s) <= DEFAULT_SPEECH_LIMIT + 10 for s in spoken)


def test_unpunctuated_text_is_clamped_not_passed_through():
    """Regression: a pasted dump with no .!? has one "sentence", which the
    splitter must still bound — otherwise it is one huge synthesis call."""
    for text in ("x" * 5000, "x" * 900 + ". tail. " + "y" * 900 + "."):
        spoken = split_sentences(text, limit=150)
        assert spoken, text[:20]
        assert all(len(piece) <= 160 for piece in spoken), [len(p) for p in spoken]


def test_first_sentence_is_kept_even_when_later_ones_are_dropped():
    spoken = split_sentences("a" * 400 + ". b c d.", limit=150)
    assert spoken[0] == "a" * 150 + "…"
