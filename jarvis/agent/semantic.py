"""Semantic memory (roadmap #5): remember what was *said*, not just what was
dictated.

``jarvis.agent.memory`` already stores every episode (every user line and
every spoken answer) in SQLite, but retrieval is recency-only: the last four
turns. So a fact mentioned three weeks ago might as well never have been
said. This module adds the missing tier: episodes get embedded (lazily, in
the background) into vectors in the same database, and every turn can ask
"which past conversations are relevant to this one?".

Design choices that matter:

* **Embedding is enrichment, never a gate.** Writes stay on the fast,
  dependency-free path (``Memory.remember_turn``); vectors are filled in by
  ``backfill()`` from the orb's bounded background loop, which the memory
  governor already throttles under pressure. If Ollama's embedder is down,
  episodes simply stay unembedded and nothing else changes.
* **One database, one truth.** Vectors live beside the episodes in
  ``memory.db``, keyed by ``episode_id``, so every host that shares the file
  (orb, bot, scripts) shares the semantic tier too, exactly like the
  episodic one.
* **The embedder is whatever is available** (``jarvis.rag.store``'s ladder:
  an Ollama embedding model, else a deterministic hashing bag-of-words).
  Vectors record their embedder tag; changing model invalidates the old
  vectors, which the next backfill pass re-embeds - cosine across different
  vector spaces is meaningless, and this way it can never be computed.
* **Search fuses cosine with rare-term overlap** (the same finding as the
  document RAG: a distinctive token like an access code must outrank
  vaguely-related prose, and the hashing fallback is lexical anyway).
"""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import struct
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence

_logger = logging.getLogger("jarvis.semantic")

# Episodes shorter than this carry no retrievable idea (acks, "ok", "thanks")
# and would only dilute the index.
MIN_CHARS = 14
# Cosine floor calibrated against the live store: on nomic-embed-text, real
# topical matches land 0.50-0.70 and adjacent-but-useless episodes 0.36-0.42.
# The hashing fallback leans on the lexical fusion term, which clears this
# floor only on genuine word overlap.
DEFAULT_MIN_SCORE = 0.42
# Never re-serve into the prompt what is already in the raw recency window;
# the caller passes this many recent episode ids to exclude.
RESULT_TEXT_CHARS = 220


@dataclass(slots=True)
class Recall:
    episode_id: int
    ts: float
    role: str
    text: str
    score: float

    def as_dict(self) -> dict:
        return {"episode_id": self.episode_id, "ts": self.ts, "role": self.role,
                "text": self.text, "score": round(self.score, 3)}


@dataclass(slots=True)
class _EmbedderInfo:
    tag: str
    dim: int
    embed: Callable[[str], bytes]
    embed_batch: Optional[Callable[[Sequence[str]], List[bytes]]] = None


_WORD_RE = re.compile(r"[a-z0-9']+")

# Does this utterance refer to the *past* — something said, told, asked,
# or an owned fact only the conversation history could supply? Recall is
# optional context; running its (cheap but not free) lookup on every turn
# adds noise tokens to turns that are plainly about right now.
_RECOLLECTION_RE = re.compile(
    r"\b(?:do|did) (?:you|u) (?:remember|recall|hear|see)\b"
    r"|\bdid we (?:talk|discuss|say|agree|go over)\b"
    r"|\bwhat did (?:i|we) (?:say|tell|mention|ask|discuss|agree)\b"
    r"|\bwhere did i (?:say|put|leave|tell you)\b"
    r"|\b(?:you|we) (?:mentioned|said|told|discussed) (?:earlier|before|last)\b"
    r"|\b(?:earlier|before|last time|yesterday|the other day)\b[^?.]*\b(?:i|we|you)\b"
    r"|\bremind me (?:what|where|who|why|when)\b"
    r"|\bwhat(?:'s| is) my\b|\bwhere(?:'s| is) my\b|\bwho(?:'s| is) my\b"
    r"|\bmy (?:code|pin|password|passcode)\b",
    re.I,
)


def looks_like_recollection(text: str) -> bool:
    return bool(_RECOLLECTION_RE.search(text or ""))


def _words(text: str) -> set:
    return {w for w in _WORD_RE.findall((text or "").lower()) if len(w) > 2}


def _default_embedder_factory() -> _EmbedderInfo:
    """Same ladder as the document RAG (Ollama embedding model, else the
    deterministic hasher), except the HTTP embedder gets a short keep_alive:
    the semantic index is fed by a background loop on the user's daily-drive
    laptop, and a second resident model between ticks is pure waste.
    """
    import urllib.request

    from jarvis.rag.store import HashingEmbedder, OllamaEmbedder
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3) as r:
            names = [m["name"] for m in json.loads(r.read()).get("models", [])]
        for cand in ("nomic-embed-text", "mxbai-embed-large", "all-minilm-l6-v2"):
            if any(n.startswith(cand) or cand in n for n in names):
                emb = OllamaEmbedder(cand, keep_alive="20s")
                return _EmbedderInfo(tag=cand, dim=emb.dim(), embed=emb.embed,
                                     embed_batch=emb.embed_batch)
    except Exception:
        pass
    h = HashingEmbedder()
    return _EmbedderInfo(tag="hashing", dim=h.dim if hasattr(h, "dim") else 512,
                         embed=h.embed, embed_batch=h.embed_batch)


class SemanticMemory:
    """Embedding-backed recall over the episodic table of ``memory.db``.

    Safe to construct anywhere the Memory is: a missing/locked db only makes
    every method a no-op returning empty results, matching the orb rule that
    memory must never take a turn down.
    """

    def __init__(self, db_path: Path, *, embedder_factory: Optional[Callable[[], _EmbedderInfo]] = None,
                 min_score: float = DEFAULT_MIN_SCORE,
                 lexical_gate: Optional[Callable[[], bool]] = None) -> None:
        """`lexical_gate` is a host-provided predicate: when it returns True
        (memory pressure on a small machine), search() degrades to a
        zero-model lexical scan instead of loading an embedding model for a
        *nice-to-have* context block. The host that already OOM-killed once
        while nomic loaded during a turn configures this on purpose.
        """
        self.db_path = Path(db_path)
        self.min_score = float(min_score)
        self.lexical_gate = lexical_gate
        self._factory = embedder_factory
        self._embedder: Optional[_EmbedderInfo] = None
        self._lock = threading.Lock()  # one embedder attempt per instance lifetime per call site
        self._embedder_failed_at = 0.0
        self._init()

    # ------------------------------------------------------------- storage
    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS episode_vectors(
                    episode_id INTEGER PRIMARY KEY,
                    model    TEXT NOT NULL,
                    dim      INTEGER NOT NULL,
                    vec      BLOB NOT NULL,
                    embedded_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS semantic_meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    # ------------------------------------------------------------- embedder
    def _get_embedder(self) -> Optional[_EmbedderInfo]:
        """Lazy; a machine without Ollama running should not pay a retry per
        call (backoff) but should recover when the engine comes back."""
        if self._embedder is not None:
            return self._embedder
        if time.time() - self._embedder_failed_at < 30.0:
            return None
        with self._lock:
            if self._embedder is not None:
                return self._embedder
            try:
                factory = self._factory or _default_embedder_factory
                self._embedder = factory()
            except Exception as exc:
                self._embedder_failed_at = time.time()
                _logger.debug("semantic embedder unavailable: %s", exc)
                return None
        return self._embedder

    def _rotate_if_model_changed(self, conn, info: _EmbedderInfo) -> None:
        row = conn.execute("SELECT value FROM semantic_meta WHERE key='model'").fetchone()
        if row and row["value"] != info.tag:
            conn.execute("DELETE FROM episode_vectors")
            _logger.info("semantic: embedder changed %s -> %s, vectors reset for re-embed",
                         row["value"], info.tag)
        conn.execute("INSERT INTO semantic_meta(key, value) VALUES('model', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (info.tag,))

    # ------------------------------------------------------------- ingestion
    def pending_ids(self, limit: int = 64, model: Optional[str] = None) -> List[int]:
        """Episodes without a vector. With a `model` given, only a vector for
        THAT embedder counts as embedded: a model swap re-embeds everything,
        because cosine across spaces is noise. Without it, any vector counts.
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """SELECT e.id FROM episodes e
                       LEFT JOIN episode_vectors v
                           ON v.episode_id = e.id AND (? IS NULL OR v.model = ?)
                       WHERE v.episode_id IS NULL AND length(e.content) >= ?
                       ORDER BY e.id DESC LIMIT ?""",
                    (model, model, MIN_CHARS, int(limit)),
                ).fetchall()
            return [r["id"] for r in rows]
        except sqlite3.Error:
            return []

    def backfill(self, batch: int = 16) -> int:
        """Embed up to `batch` unembedded episodes. Returns how many were
        stored; 0 means nothing pending *or* no embedder (check stats)."""
        info = self._get_embedder()
        if info is None:
            return 0
        ids = self.pending_ids(limit=batch, model=info.tag)
        if not ids:
            return 0
        embedded = 0
        # Read + embed OUTSIDE any write transaction: sqlite blocks readers
        # while a writer holds the lock, and an HTTP round trip to Ollama
        # must never sit inside one (a concurrent turn writes episodes).
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id, content FROM episodes WHERE id IN ({','.join('?' * len(ids))})",
                ids,
            ).fetchall()
        texts = [r["content"][:2000] for r in rows]
        try:
            if info.embed_batch is not None:
                # One HTTP round trip per tick, not one per episode:
                # fewer keep_alive edge cases and far less total time.
                vecs = info.embed_batch(texts)
            else:
                vecs = [info.embed(t) for t in texts]
        except Exception as exc:
            _logger.debug("semantic batch embed failed: %s", exc)
            return 0
        with self._connect() as conn:
            self._rotate_if_model_changed(conn, info)
            now = time.time()
            for r, vec in zip(rows, vecs):
                conn.execute(
                    "INSERT OR REPLACE INTO episode_vectors(episode_id, model, dim, vec, embedded_at)"
                    " VALUES (?,?,?,?,?)",
                    (r["id"], info.tag, info.dim, vec, now),
                )
                embedded += 1
        return embedded

    def reset_vectors(self) -> int:
        with self._connect() as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM episode_vectors").fetchone()["c"]
            conn.execute("DELETE FROM episode_vectors")
        return int(n)

    # --------------------------------------------------------------- search
    def search(self, query: str, k: int = 4, *, min_score: Optional[float] = None,
               exclude_recent: int = 8) -> List[Recall]:
        """Top-k episodes by fused cosine + rare-term overlap.

        `exclude_recent` skips the newest N episodes: they are already in the
        model's raw conversation window, and quoting them back as "a memory"
        is noise that costs prompt tokens.

        Under the host's lexical gate the embedding path is not touched at
        all: query embedding would load a whole extra model just to enrich
        optional context, and on a small machine that enrichment has been
        observed to cost the backend its life.
        """
        if self.lexical_gate is not None:
            try:
                if self.lexical_gate():
                    return self.lexical_search(query, k, exclude_recent=exclude_recent)
            except Exception:
                pass
        q = (query or "").strip()
        if len(q) < 3:
            return []
        info = self._get_embedder()
        if info is None:
            return []
        floor = self.min_score if min_score is None else float(min_score)
        try:
            qvec = struct.unpack(f"{info.dim}f", info.embed(q[:2000]))
        except Exception as exc:
            _logger.debug("semantic query embed failed: %s", exc)
            return []
        qn = math.sqrt(sum(x * x for x in qvec)) or 1.0
        qw = _words(q)
        hits: List[tuple] = []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """SELECT v.episode_id, v.vec, v.dim, e.ts, e.role, e.content
                       FROM episode_vectors v JOIN episodes e ON e.id = v.episode_id
                       WHERE v.model = ? AND e.id <= (SELECT MAX(id) FROM episodes) - ?""",
                    (info.tag, max(0, int(exclude_recent))),
                ).fetchall()
        except sqlite3.Error:
            return []
        # Corpus rarity for the lexical term weighting is handled in closed
        # form per document, so no second pass is needed for a few thousand rows.
        for r in rows:
            if len(r["vec"]) != r["dim"] * 4:
                continue  # row written by a mismatched embedder; skip, not fatal
            dvec = struct.unpack(f"{r['dim']}f", r["vec"])
            if len(qvec) != len(dvec):
                continue
            dn = math.sqrt(sum(x * x for x in dvec)) or 1.0
            cos = sum(a * b for a, b in zip(qvec, dvec)) / (qn * dn)
            dw = _words(r["content"])
            if qw and dw:
                overlap = qw & dw
                # distinctive terms score higher than "the/weather/have"
                common = (qw | dw) - overlap
                lex = len(overlap) / (len(overlap) + 0.5 * len(common) + 1.0)
            else:
                lex = 0.0
            score = 0.75 * max(0.0, cos) + 0.25 * min(1.0, lex * 2.0)
            if score >= floor:
                hits.append((score, r))
        hits.sort(key=lambda t: t[0], reverse=True)
        out: List[Recall] = []
        for score, r in hits[:max(1, int(k))]:
            text = re.sub(r"\s+", " ", r["content"][:RESULT_TEXT_CHARS]).strip()
            out.append(Recall(r["episode_id"], float(r["ts"]), r["role"], text, score))
        return out

    # ---------------------------------------------------------------- misc
    # ---------------------------------------------------------------- misc
    def lexical_search(self, query: str, k: int = 4, *, exclude_recent: int = 8,
                       lookback: int = 4000) -> List[Recall]:
        """Zero-model fallback: rare-word overlap over recent episode text.

        Catches exactly the recall questions that matter when there is no
        headroom for an embedder - "what is my elevator code" shares the
        distinctive tokens with the episode that said it. Weaker on pure
        paraphrase; costs nothing and cannot load a model.
        """
        qw = {w for w in _words(query) if w not in _STOP}
        if not qw:
            return []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """SELECT id, ts, role, content FROM episodes
                       WHERE id <= (SELECT MAX(id) FROM episodes) - ?
                       ORDER BY id DESC LIMIT ?""",
                    (max(0, int(exclude_recent)), int(lookback)),
                ).fetchall()
        except sqlite3.Error:
            return []
        scored = []
        for r in rows:
            dw = _words(r["content"]) - _STOP
            if not dw:
                continue
            overlap = qw & dw
            if not overlap:
                continue
            score = len(overlap) / (len(qw) + 0.5 * len(dw - qw))
            scored.append((score, r))
        scored.sort(key=lambda t: t[0], reverse=True)
        out = []
        for score, r in scored[:max(1, int(k))]:
            text = re.sub(r"\s+", " ", r["content"][:RESULT_TEXT_CHARS]).strip()
            out.append(Recall(r["id"], float(r["ts"]), r["role"], text, score))
        return out

    def stats(self) -> dict:
        try:
            with self._connect() as conn:
                embedded = conn.execute("SELECT COUNT(*) AS c FROM episode_vectors").fetchone()["c"]
                episodes = conn.execute("SELECT COUNT(*) AS c FROM episodes").fetchone()["c"]
                model = conn.execute("SELECT value FROM semantic_meta WHERE key='model'").fetchone()
            return {"episodes": episodes, "embedded": embedded,
                    "pending": max(0, episodes - embedded),
                    "model": model["value"] if model else None}
        except sqlite3.Error:
            return {"episodes": 0, "embedded": 0, "pending": 0, "model": None}

    def recall_prompt_block(self, recalls: Sequence[Recall]) -> str:
        """Format search hits as a system block. Deliberately hedged: an old
        episode is evidence of what was said, not proof of what is true now
        ("I'm booking a flight" last month is not a current flight)."""
        if not recalls:
            return ""
        lines = []
        for rc in recalls:
            when = time.strftime("%Y-%m-%d", time.localtime(rc.ts))
            speaker = "the user" if rc.role == "user" else "you"
            lines.append(f"- ({when}, {speaker} said): {rc.text}")
        return (
            "Possibly-relevant fragments from past conversations (they may be "
            "outdated; treat them as leads worth mentioning only if they "
            "clearly answer the question, never as current fact). If one "
            "fragment contains the exact thing asked for - a code, number, "
            "name or date - state it directly and say when it was said:\n"
            + "\n".join(lines)
        )


_STOP = {"the", "and", "was", "are", "for", "that", "this", "with", "have", "has",
         "you", "your", "my", "me", "is", "it", "to", "of", "in", "on", "at", "do",
         "did", "what", "when", "where", "which", "about", "tell", "said", "say",
         "remember", "recall", "can", "could", "would", "just", "like", "get", "got"}


__all__ = ["SemanticMemory", "Recall", "MIN_CHARS", "DEFAULT_MIN_SCORE",
           "looks_like_recollection"]
