import time

import pytest

from jarvis.agent.user_model import explicit_statement

from jarvis.agent.user_model import UserModel, _same_enough, extract_facts


@pytest.fixture
def model(tmp_path):
    return UserModel(tmp_path / "memory.db")


def test_facts_appear_in_the_prompt_block(model):
    model.put("address_as", "Call me JB, never 'sir'.")
    model.put("timezone", "User is in America/Toronto.")
    block = model.prompt_block()
    assert "Call me JB" in block and "America/Toronto" in block
    assert model.prompt_block().count("- ") == 2


def test_empty_model_produces_no_prompt_block(model):
    assert model.prompt_block() == ""


def test_re_stating_a_fact_with_new_words_does_not_duplicate_it(model):
    model.put("projects", "Working on JARVIS, a local voice assistant.")
    model.merge([("projects", "I am working on JARVIS, a local voice assistant")])
    active = model.active_facts()
    assert len(active) == 1
    assert model.stats()["changes"] == 0, "a rephrase is not a change"


def test_changed_fact_supersedes_and_keeps_history(model):
    model.put("employer", "I work at Acme.")
    model.merge([("employer", "I work at Globex now.")])
    active = model.active_facts()
    assert len(active) == 1
    assert "Globex" in active[0].value
    assert "Acme" not in active[0].value
    history = model.history("employer")
    assert len(history) == 1, "one supersede must be one recorded change"
    assert history[0]["old_value"] == "I work at Acme."
    assert "Globex" in history[0]["new_value"]
    assert history[0]["reason"] == "superseded"
    # The old value lives in history, not as a duplicate hidden row.
    assert model.stats()["retired"] == 0
    assert model.stats()["changes"] == 1


def test_retiring_a_fact_removes_it_from_prompts_but_not_from_history(model):
    model.put("nickname", "Call me Captain.")
    assert model.retire("nickname") is True
    assert model.prompt_block() == ""
    assert model.fact("nickname") is None
    assert len(model.history("nickname")) == 1
    assert model.retire("nickname") is False


def test_keys_are_normalized_so_nearly_identical_slots_collide(model):
    model.put("Preferred Voice", "Use the af_heart voice.")
    model.put("preferred-voice", "Use the am_michael voice.")
    assert len(model.active_facts()) == 1
    assert "af_heart" not in model.active_facts()[0].value


def test_extraction_only_runs_on_statements_that_carry_facts():
    calls = []

    def engine(messages):
        calls.append(messages)
        return '{"facts": [{"key": "timezone", "value": "User lives in Toronto"}]}'

    assert extract_facts("I'm in Toronto, remember that", call_engine=engine) == [
        ("timezone", "User lives in Toronto")]
    assert len(calls) == 1
    assert extract_facts("what is the capital of france", call_engine=engine) == []
    assert len(calls) == 1, "a plain question must not cost a model call"


def test_extraction_survives_garbage_and_engine_failure():
    assert extract_facts("my name is JB", call_engine=lambda m: "not json at all") == []
    assert extract_facts("my name is JB", call_engine=lambda m: '{"facts": "wrong shape"}') == []
    assert extract_facts("my name is JB", call_engine=lambda m: '{"facts": [{"key":"x"}]}') == []

    def boom(m):
        raise RuntimeError("engine down")

    assert extract_facts("I prefer short answers", call_engine=boom) == []


def test_extraction_caps_how_many_facts_one_turn_can_add():
    payload = '{"facts": [' + ",".join(
        f'{{"key":"k{i}","value":"fact number {i} about me"}}' for i in range(20)) + "]}"
    facts = extract_facts("let me tell you about my life: " + " ".join(f"thing{i}" for i in range(30)),
                         call_engine=lambda m: payload)
    assert len(facts) == 6


def test_prompt_block_is_capped_for_a_long_lived_model(model, tmp_path):
    for i in range(60):
        model.put(f"topic-{i}", f"User statement {i} about preferences.")
    block = model.prompt_block()
    assert block.count("- ") <= 14
    # most recent survive, oldest drop out of the prompt (but stay in the store)
    assert "User statement 59" in block
    assert model.stats()["active"] == 60


def test_same_enough_tolerates_wording_drift():
    assert _same_enough("User prefers short answers", "the user prefers short answers.")
    assert not _same_enough("User works at Acme", "User works at Globex")


def test_values_are_length_capped(model):
    model.put("long", "x" * 5000)
    assert len(model.active_facts()[0].value) <= 220


def test_put_rejects_empty_facts(model):
    with pytest.raises(ValueError):
        model.put("", "value")
    with pytest.raises(ValueError):
        model.put("key", "   ")


def test_model_and_episodes_share_one_database_file(tmp_path):
    from jarvis.agent.memory import Memory

    db = tmp_path / "memory.db"
    memory = Memory(db)
    user_model = UserModel(db)
    memory.remember_turn("user", "call me JB, I work nights")
    user_model.put("shift", "User works nights.")
    assert memory.stats()["episodes"] == 1
    assert user_model.stats()["active"] == 1
    assert db.exists()


# ---------------------------------------------------------------- explicit keep
# The grammar, not the extractor, has to honour "do not lose it": on 10 Nov the
# owner said "my building elevator access code is 9 3 1 4, do not lose it", the
# pattern only knew a leading "remember that", nothing was pinned, and every
# later asking got "I cannot determine that".
@pytest.mark.parametrize("text,want", [
    ("my building elevator access code is 9 3 1 4, do not lose it",
     "my building elevator access code is 9 3 1 4"),
    ("remember that my wifi password is hunter2", "my wifi password is hunter2"),
    ("please remember the parking spot is B4, level 3", "the parking spot is B4, level 3"),
    ("my passport number is X1234567 for future reference", "my passport number is X1234567"),
    ("account 4471 sort 090125, keep that in mind", "account 4471 sort 090125"),
    ("my sister's birthday is 3 March - note this down", "my sister's birthday is 3 March"),
    ("I'm vegetarian, don't forget", "I'm vegetarian"),
    ("nice, thanks! remember that I fly on Tuesdays", "I fly on Tuesdays"),
])
def test_explicit_keep_statement_is_pinned(text, want):
    assert explicit_statement(text) == want


@pytest.mark.parametrize("text", [
    "don't forget to take the bins out",        # a reminder, not a fact
    "did you remember to save my file?",        # a question
    "what is my elevator code? do not lose it",  # a question with a marker
    "what's the weather",
    "tell me a joke",
    "remind me to call mum at five",
    "I like coffee",                            # no ask to keep anything
])
def test_everyday_lines_are_not_pinned_as_facts(text):
    assert explicit_statement(text) == ""


def test_a_pinned_fact_survives_into_the_prompt_and_beats_incidental_ones(model):
    fact = model.remember_explicit("my building elevator access code is 9 3 1 4, do not lose it")
    assert fact is not None and fact.pinned
    model.put("mood", "The user is in a good mood.")       # newer, but unpinned
    block = model.prompt_block()
    assert "9 3 1 4" in block
    assert block.index("9 3 1 4") < block.index("good mood")   # pinned first


def test_re_stating_a_pinned_fact_updates_the_slot_instead_of_duplicating(model):
    first = model.remember_explicit("my wifi password is hunter2, do not lose it")
    second = model.remember_explicit("remember that my wifi password is swordfish")
    assert first.key == second.key
    assert model.active_facts()[0].value == "my wifi password is swordfish"
    assert len([f for f in model.active_facts()]) == 1


def test_episodic_store_shares_the_one_grammar(tmp_path):
    """memory.maybe_store_fact must not keep a weaker copy of the rule."""
    from jarvis.agent.memory import Memory

    m = Memory(tmp_path / "memory.db")
    assert m.maybe_store_fact("my locker combination is 12-34-56, do not lose it") \
        == "my locker combination is 12-34-56"
    assert m.maybe_store_fact("don't forget to take the bins out") is None
