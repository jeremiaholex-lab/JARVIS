from jarvis.agent.memory import Memory


def test_episodes_survive_a_reopen(tmp_path):
    db = tmp_path / "memory.db"
    m = Memory(db)
    m.remember_turn("user", "what is due Friday", engine="local (qwen3:1.7b)")
    m.remember_turn("assistant", "The JARVIS deadline is due Friday.", engine="local (qwen3:1.7b)")

    reopened = Memory(db)
    turns = reopened.recent_turns()
    assert [t.role for t in turns] == ["user", "assistant"]
    assert "Friday" in turns[1].content
    assert reopened.stats()["episodes"] == 2


def test_recent_turns_returns_oldest_first_window(tmp_path):
    m = Memory(tmp_path / "m.db")
    for i in range(20):
        m.remember_turn("user", f"msg {i}")
    tail = m.recent_turns(5)
    assert [t.content for t in tail] == [f"msg {i}" for i in range(15, 20)]


def test_explicit_fact_is_stored_once_not_every_message(tmp_path):
    m = Memory(tmp_path / "m.db")
    assert m.maybe_store_fact("remember that the staging API key rotates monthly") == \
        "the staging API key rotates monthly"
    assert m.maybe_store_fact("what is the weather") is None
    assert m.maybe_store_fact("do you remember that we shipped?") is None
    assert m.facts() == ["the staging API key rotates monthly"]


def test_blank_and_bad_roles_are_ignored(tmp_path):
    m = Memory(tmp_path / "m.db")
    m.remember_turn("user", "")
    m.remember_turn("system", "not allowed")
    assert m.recent_turns() == []
