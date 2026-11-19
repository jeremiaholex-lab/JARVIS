"""JARVIS Phase 4 — RAG over the user's own documents (design §6.8).

Local-only: a deterministic hashing embedder (zero-download; upgrades to an
Ollama embedding model automatically if one is present) + a SQLite vector
store over user-chosen folders. Agents search it during turns and cite
the source document.
"""
