from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server


def test_short_answers_are_one_sentence():
    assert server.split_sentences("The capital is Paris.") == ["The capital is Paris."]


def test_all_sentences_are_preserved_for_speech():
    long_answer = " ".join(f"Sentence number {i} explains something useful." for i in range(12))
    spoken = server.split_sentences(long_answer)
    assert len(spoken) == 12
    assert " ".join(spoken) == long_answer


def test_punctuation_keeps_following_words_and_sentences():
    text = "Try this: first, second; third. Then another! Finally? Done."
    spoken = server.split_sentences(text)
    assert " ".join(spoken) == text
    assert spoken == ["Try this: first, second; third.", "Then another!", "Finally?", "Done."]


def test_long_sentence_chunks_preserve_punctuation_and_words():
    text = " ".join([*("word," for _ in range(40)), "finish.", "Afterwards, keep going!"])
    spoken = server.split_sentences(text)
    assert " ".join(spoken) == text
    assert all(len(piece) <= 150 for piece in spoken)


def test_empty_answer_speaks_nothing():
    assert server.split_sentences("") == []
    assert server.split_sentences("   ") == []


def test_long_unpunctuated_text_is_split_without_dropping_words():
    text = " ".join(f"word{i}" for i in range(100))
    spoken = server.split_sentences(text)
    assert len(spoken) > 1
    assert all(len(piece) <= 150 for piece in spoken)
    assert " ".join(spoken) == text


def test_explicit_speech_summary_still_trims_at_sentence_boundary():
    text = "Alpha beta gamma. " * 30
    out = server.speech_text(text)
    assert len(out) <= 260
    assert out.endswith(".")
