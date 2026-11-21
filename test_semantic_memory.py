"""Semantic memory (roadmap #5): lazy embedding, fused search, and the
recall_memory tool contract.

A deterministic vocabulary embedder stands in for Ollama: vectors are
one-hot per word, so cosine is exact overlap and expectations are stable."""
from __future__ import annotations

import json
import math
import sqlite3
import struct
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis.agent.memory import Memory, shared_memory_path  # noqa: E402
from jarvis.agent.semantic import MIN_CHARS, Recall, SemanticMemory, _EmbedderInfo  # noqa: E402
from jarvis.security.tiers import PlannedAction, Tier, classify  # noqa: E402

VOCAB = ["access", "code", "elevator", "lease", "apartment", "flight",
         "paris", "deadline", "project", "oracle", "banana", "weather"]
IDX = {w: i for i, w in enumerate(VOCAB)}


def vocab_embedder(tag="fake-1"):
    def embed(text: str) -> bytes:
        v = [0.0] * len(VOCAB)
        for w in text.lower().split():
            w = w.strip(".,!?;:\"'")
            if w in IDX:
                v[IDX[w]] = 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return struct.pack(f"{len(v)}f", *[x / n for x in v])
    return _EmbedderInfo(tag=tag, dim=len(VOCAB), embed=embed)


EPISODES = [
    ("user", "my elevator access code is 4 7 2 9, don't lose it"),
    ("assistant", "noted, the elevator code is kept in this conversation only"),
    ("user", "the lease deadline is the first of next month"),
    ("assistant", "your apartment lease renews then"),
    ("user", "flying to paris on friday for the oracle deadline"),
    ("assistant", "weather in paris friday looks mild"),
]


@pytest.fixture
def mem(tmp_path):
    """A memory.db with a handful of episodes and nothing embedded yet."""
    db = tmp_path / "memory.db"
    store = Memory(db)
    for role, text in EPISODES:
        store.remember_turn(role, text)
    sem = SemanticMemory(db, embedder_factory=vocab_embedder)
    return store, sem


def test_remember_turn_returns_rowid(mem):
    store, _ = mem
    before = store.remember_turn("user", "a fresh line worth embedding")
    assert isinstance(before, int) and before > 0
    assert store.remember_turn("user", "") is None
    assert store.remember_turn("system", "nope") is None


def test_pending_skips_short_and_embedded(mem):
    store, sem = mem
    store.remember_turn("user", "ok")  # below MIN_CHARS
    pend = sem.pending_ids()
    assert len(pend) == len(EPISODES)
    assert sem.backfill(batch=99) == len(EPISODES)
    assert sem.pending_ids() == []


def test_backfill_is_idempotent_and_bounded(mem):
    _, sem = mem
    assert sem.backfill(batch=2) == 2
    assert sem.backfill(batch=2) == 2
    assert sem.backfill(batch=99) == len(EPISODES) - 4
    assert sem.backfill() == 0


def test_search_ranks_relevant_episode_first(mem):
    _, sem = mem
    sem.backfill(batch=99)
    hits = sem.search("elevator access code", k=3, exclude_recent=0)
    assert hits, "expected at least one hit"
    assert "elevator" in hits[0].text and hits[0].role == "user"
    assert hits[0].score >= hits[-1].score
    # an unrelated query finds nothing above the floor
    assert sem.search("quantum tunnelling", k=3, exclude_recent=0) == []


def test_exclude_recent_skips_the_live_window(mem):
    store, sem = mem
    sem.backfill(batch=99)
    # newest 6 episodes hidden: the elevator code (id 1) must still surface
    assert sem.search("elevator code", k=3, exclude_recent=0)
    assert sem.search("paris weather", k=3, exclude_recent=6) == []


def test_embedder_model_change_invalidates_vectors(tmp_path):
    db = tmp_path / "memory.db"
    store = Memory(db)
    for role, text in EPISODES:
        store.remember_turn(role, text)
    sem = SemanticMemory(db, embedder_factory=lambda: vocab_embedder("fake-a"))
    sem.backfill(batch=99)
    assert sem.search("elevator access code", k=2, exclude_recent=0)
    # new instance, new embedder tag: old vectors must never be compared
    swapped = SemanticMemory(db, embedder_factory=lambda: vocab_embedder("fake-b"))
    hits = swapped.search("elevator access code", k=2, exclude_recent=0)
    assert hits == []  # WHERE v.model = ? filters the stale rows
    swapped.backfill(batch=99)  # rotation drops old rows and re-embeds
    con = sqlite3.connect(db)
    models = {r[0] for r in con.execute("SELECT DISTINCT model FROM episode_vectors")}
    con.close()
    assert models == {"fake-b"}
    assert swapped.search("elevator access code", k=2, exclude_recent=0)


def test_mismatched_vector_blob_is_skipped_not_fatal(mem):
    _, sem = mem
    sem.backfill(batch=99)
    with sqlite3.connect(sem.db_path) as con:
        con.execute("UPDATE episode_vectors SET vec=x'0011' WHERE episode_id=(SELECT MIN(episode_id) FROM episode_vectors)")
    hits = sem.search("lease apartment", k=3, exclude_recent=0)
    assert hits and all("lease" in h.text or "apartment" in h.text for h in hits)


def test_recognises_empty_and_short_queries(mem):
    _, sem = mem
    assert sem.search("", exclude_recent=0) == []
    assert sem.search("hi", exclude_recent=0) == []  # < 3 chars
    sem.backfill(batch=99)
    assert sem.search("ok", exclude_recent=0) == []


def test_prompt_block_formatting_hedges(mem):
    _, sem = mem
    assert sem.recall_prompt_block([]) == ""
    block = sem.recall_prompt_block([
        Recall(1, time.time(), "user", "the deadline is friday", 0.7),
        Recall(2, time.time(), "assistant", "noted", 0.5),
    ])
    assert "the deadline is friday" in block
    assert "the user said" in block and "you said" in block
    assert "outdated" in block.lower()


def test_no_embedder_degrades_quietly(tmp_path):
    db = tmp_path / "memory.db"
    Memory(db).remember_turn("user", "elevator code four seven")
    boom = lambda: (_ for _ in ()).throw(ConnectionError("no ollama"))
    sem = SemanticMemory(db, embedder_factory=boom)
    assert sem.backfill() == 0
    assert sem.search("elevator code", exclude_recent=0) == []
    st = sem.stats()
    assert st["pending"] >= 1 and st["embedded"] == 0


def test_stats_counts(mem):
    _, sem = mem
    st = sem.stats()
    assert st["episodes"] == len(EPISODES) and st["pending"] == len(EPISODES)
    sem.backfill(batch=99)
    st = sem.stats()
    assert st["embedded"] == len(EPISODES) and st["model"] == "fake-1"


# ----------------------------------------------------------- tool + tier glue
def test_recall_memory_tool_executes(tmp_path):
    import jarvis.agent.tools as tools
    db = tmp_path / "memory.db"
    store = Memory(db)
    store.remember_turn("user", "my elevator access code is 4 7 2 9, don't lose it")
    # The tool excludes the newest 8 episodes by design (they are already in
    # the model's raw conversation window), so age the target episode out.
    for i in range(9):
        store.remember_turn("user", f"filler episode number {i} about nothing")
    sem = SemanticMemory(db, embedder_factory=vocab_embedder)
    sem.backfill(batch=99)
    old, tools._SEM_CACHE = getattr(tools, "_SEM_CACHE", None), sem
    try:
        cat = tools.build_tools()
        _ = next(t for t in cat if t.name == "recall_memory")
        out = json.loads(tools.execute(cat, "recall_memory",
                                       json.dumps({"query": "elevator access code"})))
        assert out["results"] and "elevator" in out["results"][0]["text"]
        assert "outdated" in out["note"]
    finally:
        tools._SEM_CACHE = old


def test_recall_memory_is_read_tier():
    assert classify(PlannedAction("recall_memory", {"query": "x"})) is Tier.READ


def test_unknown_embedding_never_exposes_write():
    # the tier list addition must not have flipped the default for anything else
    assert classify(PlannedAction("recall_memory_writer", {})) is Tier.WRITE


# ------------------------------------------------- degradation + intent layer
def test_recollection_intent():
    from jarvis.agent.semantic import looks_like_recollection as L
    for yes in ["what did I tell you about the lease", "do you remember my elevator code",
                "what is my access code", "where did I say I put the lease",
                "remind me what we decided last time", "what's my plant watering day",
                "did we talk about the oracle deadline", "my pin for the elevator?"]:
        assert L(yes), yes
    for no in ["what is the tallest building", "remind me to call mom at five",
               "open the lease file", "what's the weather", "set a timer for ten minutes",
               "turn up the screen brightness"]:
        assert not L(no), no


def test_lexical_search_needs_no_embedder(tmp_path):
    db = tmp_path / "memory.db"
    store = Memory(db)
    store.remember_turn("user", "my building elevator access code is 4729")
    for i in range(10):
        store.remember_turn("user", f"routine filler conversation entry {i}")
    boom = lambda: (_ for _ in ()).throw(ConnectionError("no embedder allowed"))
    sem = SemanticMemory(db, embedder_factory=boom, lexical_gate=lambda: True)
    hits = sem.search("what is my elevator access code", k=3)
    assert hits and "4729" in hits[0].text


def test_gate_off_uses_vector_space(mem):
    _, sem = mem
    sem.backfill(batch=99)
    sem.lexical_gate = lambda: False
    hits = sem.search("elevator access code", k=2, exclude_recent=0)
    assert hits and "elevator" in hits[0].text


def test_stopwords_do_not_dominate_lexical(mem):
    store, sem = mem
    hits = sem.lexical_search("what is my lease deadline", k=2, exclude_recent=0)
    assert hits and "lease" in hits[0].text.lower()


def test_default_governor_singleton():
    from jarvis.agent.governor import default_governor
    assert default_governor() is default_governor()
