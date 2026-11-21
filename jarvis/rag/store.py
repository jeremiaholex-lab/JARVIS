"""Local embedding + SQLite vector store over user-chosen folders (§6.8).

Embedder strategy (all local, zero-download):
  1. Ollama embedding model, if one is installed (nomic-embed-text, mxbai-…).
  2. Otherwise a deterministic hashing embedder (hashed bag-of-words into a
     fixed-dim vector, L2-normalized). Not a learned model, but it is
     reproducible, offline, and good enough for keyword-dominant personal
     docs — the design target is the user's own project docs, not
     cross-lingual semantic search.

The store is SQLite with the design's §6.5/§6.8 shape: a `chunks` table
(content, source path, hash, mtime) + a `vectors` table (one BLOB per
chunk). No external vector DB.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

DIM = 512  # hashing-embedder dimension (framework store default too)
CHUNK_CHARS = 400
CHUNK_OVERLAP = 80
_TEXT_EXT = {".md", ".txt", ".py", ".js", ".ts", ".tsx", ".html", ".json", ".toml", ".yaml", ".yml", ".csv", ".ini", ".cfg"}
_SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "bin", "obj", "dist", ".openjarvis"}
# Vendor/library trees that must never shadow the user's own documents when
# the user's project root is indexed. Add your own here.
_SKIP_NAMES = {"openjarvis_runtime", "openjarvis", "orb", "generated-images", ".venv"}
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "did", "do",
    "does", "for", "from", "had", "has", "have", "how", "i", "in", "is",
    "it", "its", "of", "on", "or", "our", "that", "the", "their", "then",
    "these", "they", "this", "to", "was", "were", "which", "while", "with",
}


# ---------------------------------------------------------------------------
# Embedder
# ---------------------------------------------------------------------------

class HashingEmbedder:
    """Deterministic offline embedder: hashed bag-of-words, L2-normalized."""

    name = "hashing-bow-512"

    def _tokens(self, text: str) -> List[str]:
        words = [w for w in text.lower().split() if len(w) > 2]
        return words or [text.lower()[:16]]

    def embed(self, text: str) -> bytes:
        vec = [0.0] * DIM
        for w in self._tokens(text):
            # FNV-1a -> bucket + sign (the "signed hashing trick").
            h = _fnv1a(w)
            bucket = h % DIM
            sign = 1.0 if (h >> 63) & 1 else -1.0
            vec[bucket] += sign
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return struct.pack(f"{DIM}f", *[x / norm for x in vec])

    def embed_batch(self, texts: Sequence[str]) -> List[bytes]:
        return [self.embed(t) for t in texts]


def _fnv1a(s: str) -> int:
    h = 0xCBF29CE484222325
    for b in s.encode("utf-8"):
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


class OllamaEmbedder:
    """Uses an Ollama embedding model when one is installed.

    ``keep_alive`` bounds how long Ollama retains the model in RAM after a
    call; background indexers on a small machine pass a short value so a
    300 MB embedding model does not sit resident between ticks."""

    def __init__(self, model: str, keep_alive: Optional[str] = None) -> None:
        self.model = model
        self.keep_alive = keep_alive
        import urllib.request

        self._urllib = urllib.request

    def _post(self, payload: dict):
        if self.keep_alive:
            payload = {**payload, "keep_alive": self.keep_alive}
        req = self._urllib.Request(
            "http://localhost:11434/api/embed",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with self._urllib.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

    def embed(self, text: str) -> bytes:
        v = self._post({"model": self.model, "input": text})["embeddings"][0]
        return struct.pack(f"{len(v)}f", *v)

    def embed_batch(self, texts: Sequence[str]) -> List[bytes]:
        v = self._post({"model": self.model, "input": list(texts)})["embeddings"]
        return [struct.pack(f"{len(x)}f", *x) for x in v]


    name = "ollama"

    def dim(self) -> int:
        v = self.embed("dim probe")
        return len(struct.unpack(f"{len(v) // 4}f", v))


def make_embedder() -> Tuple[object, int]:
    """Prefer an Ollama embedding model when available, else hashing."""
    try:
        import urllib.request, json as _j

        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3) as r:
            names = [m["name"] for m in _j.loads(r.read()).get("models", [])]
        for cand in ("nomic-embed-text", "mxbai-embed-large", "all-minilm-l6-v2"):
            if any(n.startswith(cand) or cand in n for n in names):
                emb = OllamaEmbedder(cand)
                return emb, emb.dim()
    except Exception:
        pass
    emb = HashingEmbedder()
    return emb, DIM


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class RAGHit:
    source: str
    snippet: str
    score: float
    heading: str = ""


class RagStore:
    """SQLite chunk + vector store over user-chosen folders."""

    def __init__(self, db_path: Path, *, folders: Sequence[Path], embedder=None) -> None:
        self.db = Path(db_path)
        self.folders = [Path(f) for f in folders]
        self.embedder, self.dim = make_embedder() if embedder is None else (embedder, DIM)
        self._init()

    def _init(self) -> None:
        self.db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id TEXT PRIMARY KEY,
                source   TEXT NOT NULL,
                heading  TEXT NOT NULL DEFAULT '',
                content  TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                mtime    REAL NOT NULL,
                indexed_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS vectors (
                chunk_id TEXT PRIMARY KEY REFERENCES chunks(chunk_id),
                vec      BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS chunks_source ON chunks(source);
            """
        )
        conn.commit()
        conn.close()

    # -- indexing ---------------------------------------------------------
    def index(self, force: bool = False) -> int:
        """Walk the configured folders; index/refresh missing, changed, or
        deleted files. Returns number of chunks written this pass."""
        seen: set[str] = set()
        written = 0
        conn = sqlite3.connect(self.db)
        files = [p for p in self._walk()]
        for path in files:
            try:
                mtime = path.stat().st_mtime
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            file_hash = hashlib.sha1(text.encode("utf-8")).hexdigest()
            row = conn.execute(
                "SELECT file_hash FROM chunks WHERE source = ? LIMIT 1", (str(path),)
            ).fetchone()
            if row and row[0] == file_hash and not force:
                continue
            # Purge old chunks for this file, then re-chunk.
            conn.execute("DELETE FROM vectors WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE source = ?)", (str(path),))
            conn.execute("DELETE FROM chunks WHERE source = ?", (str(path),))
            chunks = _chunk_text(text, str(path))
            for chunk_id, (heading, content) in chunks.items():
                blob = self.embedder.embed(content)
                conn.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?)",
                             (chunk_id, str(path), heading, content, file_hash, mtime, time.time()))
                conn.execute("INSERT OR REPLACE INTO vectors VALUES (?,?)", (chunk_id, blob))
                seen.add(chunk_id)
                written += 1
        # Drop chunks whose files disappeared.
        for gone in conn.execute("SELECT DISTINCT source FROM chunks").fetchall():
            if not Path(gone[0]).is_file():
                ids = [r[0] for r in conn.execute("SELECT chunk_id FROM chunks WHERE source=?", (gone[0],))]
                for cid in ids:
                    conn.execute("DELETE FROM vectors WHERE chunk_id=?", (cid,))
                conn.execute("DELETE FROM chunks WHERE source=?", (gone[0],))
        conn.commit()
        conn.close()
        return written

    def _walk(self):
        for folder in self.folders:
            if not folder.is_dir():
                continue
            for p in folder.rglob("*"):
                if p.is_dir() or any(part in _SKIP_DIRS or part in _SKIP_NAMES for part in p.parts):
                    continue
                if p.suffix in _TEXT_EXT and p.stat().st_size < 2_000_000:
                    yield p

    # -- search -----------------------------------------------------------
    def search(self, query: str, *, top_k: int = 5) -> List[RAGHit]:
        """Weighted fusion of two local signals (no trained model):
          1. embedding cosine (hashing bag-of-words, or Ollama if present)
          2. query-time TF-IDF — rare distinctive terms ("ultron") dominate
             over common ones ("the"), so the single chunk that actually
             answers the question ranks first.
        Both are min-max normalized to [0,1], then fused 3:1 in favor of the
        keyword signal, so a question whose answer exists in one document
        surfaces that document at rank 1."""
        conn = sqlite3.connect(self.db)
        qvec = struct.unpack(f"{self.dim}f", self.embedder.embed(query))
        rows = conn.execute("SELECT chunk_id, source, heading, content FROM chunks").fetchall()
        vecs = dict(conn.execute("SELECT chunk_id, vec FROM vectors").fetchall())
        if not rows:
            # An unbuilt or emptied index is a normal state, not a crash.
            conn.close()
            return []

        # --- signal 1: embedding cosine ---
        cos_scores: List[Tuple[float, str]] = []
        for chunk_id, _source, _heading, _content in rows:
            blob = vecs.get(chunk_id)
            if not blob:
                continue
            v = struct.unpack(f"{self.dim}f", blob)
            cos_scores.append((sum(a * b for a, b in zip(qvec, v)), chunk_id))

        # --- signal 2: TF-IDF over the indexed chunks ---
        df: dict[str, int] = {}
        tf_rows: List[Tuple[str, dict]] = []
        for chunk_id, _source, _heading, content in rows:
            tf: dict[str, int] = {}
            for w in _term_tokens(content):
                tf[w] = tf.get(w, 0) + 1
            tf_rows.append((chunk_id, tf))
            for w in tf:
                df[w] = df.get(w, 0) + 1
        n = len(rows) or 1
        # idf**2 makes rare, distinctive terms ("ultron") dominate the ranking
        # over common ones ("the", "document"), which is exactly what a
        # "this fact lives in one doc" question needs.
        idf = {w: (math.log((n + 1) / (d + 1)) + 1.0) ** 2 for w, d in df.items()}
        qtf = {w: 0 for w in _term_tokens(query)}
        for w in _term_tokens(query):
            qtf[w] = qtf.get(w, 0) + 1
        tfidf_scores: List[Tuple[float, str]] = []
        for chunk_id, tf in tf_rows:
            score = 0.0
            for w, q in qtf.items():
                score += q * idf.get(w, 0.0) * tf.get(w, 0)
            tfidf_scores.append((score, chunk_id))

        conn.close()

        # --- fusion: TF-IDF dominant, cosine as tie-breaker ---
        # The done-test property: a question whose answer exists in ONE
        # document must surface that document first. Raw TF-IDF already does
        # this (rare distinctive terms like "ultron" have huge IDF and blow
        # past common terms); min-max normalizing it instead would let a
        # chunk that wins only on dense-word cosine steal the top slot.
        # So: sort by TF-IDF score, break ties with the embedding cosine.
        def _norm(scored: List[Tuple[float, str]]) -> dict[str, float]:
            if not scored:
                return {}
            vals = [s for s, _ in scored]
            lo, hi = min(vals), max(vals)
            span = hi - lo
            return {
                cid: (0.0 if span == 0 else (v - lo) / span)
                for v, cid in scored
            }

        tfidf_by_id = {cid: score for score, cid in tfidf_scores}
        tf_by_id = {cid: tf for cid, tf in tf_rows}
        cos_norm = _norm(cos_scores)
        meta = {c: (s, h, ct) for c, s, h, ct in rows}
        top = sorted(
            meta,
            key=lambda cid: (-tfidf_by_id.get(cid, 0.0), -cos_norm.get(cid, 0.0)),
        )[:top_k]
        hits: List[RAGHit] = []
        for chunk_id in top:
            source, heading, content = meta[chunk_id]
            # Center the snippet on the chunk's rarest matched query term,
            # so the cited text visibly contains the distinctive answer.
            tf = tf_by_id.get(chunk_id, {})
            hint = ""
            best_idf = 0.0
            for w in qtf:
                if tf.get(w) and idf.get(w, 0.0) > best_idf:
                    best_idf = idf.get(w, 0.0)
                    hint = w
            hits.append(
                RAGHit(
                    source=source,
                    snippet=_snippet(content, hint=hint),
                    score=round(float(tfidf_by_id.get(chunk_id, 0.0)), 5),
                    heading=heading,
                )
            )
        return hits

    def stats(self) -> dict:
        conn = sqlite3.connect(self.db)
        n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        n_files = conn.execute("SELECT COUNT(DISTINCT source) FROM chunks").fetchone()[0]
        conn.close()
        return {"chunks": n_chunks, "files": n_files, "embedder": getattr(self.embedder, "name", "unknown"), "dim": self.dim}


def _chunk_text(text: str, source: str) -> dict[str, Tuple[str, str]]:
    chunks: dict[str, Tuple[str, str]] = {}
    lines = text.splitlines()
    heading = ""
    buf: list[str] = []
    pos = 0
    i = 0
    while i < len(lines) or buf:
        if i < len(lines):
            line = lines[i]
            if line.startswith("# "):
                heading = line[2:].strip()
            buf.append(line)
            pos += len(line) + 1
            i += 1
        if pos >= CHUNK_CHARS or (i >= len(lines) and buf):
            chunk = "\n".join(buf)
            cid = hashlib.sha1(f"{source}:{hashlib.sha1(chunk.encode()).hexdigest()[:8]}".encode()).hexdigest()[:16]
            chunks[cid] = (heading, chunk)
            # keep the tail as overlap for the next window
            tail = chunk[-CHUNK_OVERLAP:] if i < len(lines) else ""
            buf = tail.split("\n") if tail else []
            pos = len(tail)
    return chunks


def _snippet(content: str, hint: str = "", max_chars: int = 300) -> str:
    """Snippet that, when a *hint* term is known to occur in the chunk,
    centers the window on its first occurrence so the cited text visibly
    contains the answer rather than ending just before it."""
    s = " ".join(content.split())
    if hint:
        idx = s.lower().find(hint.lower())
        if idx >= 0:
            start = max(0, idx - max_chars // 4)
            out = s[start:start + max_chars]
            if start > 0:
                out = "…" + out
            if start + max_chars < len(s):
                out = out + "…"
            return out
    return s if len(s) <= max_chars else s[: max_chars - 3] + "…"


def _term_tokens(text: str) -> List[str]:
    """Query/chunk terms for TF-IDF: lowercase words, stop- and punct-free."""
    import re

    return [
        w
        for w in re.findall(r"[a-z0-9][a-z0-9_\-\.]{1,}", text.lower())
        if w not in _STOPWORDS
    ]


__all__ = ["RagStore", "RAGHit", "HashingEmbedder", "OllamaEmbedder", "make_embedder"]
