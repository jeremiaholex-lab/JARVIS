"""JARVIS Phase 4 — self-evolving skills (design §6.6), restricted-process fallback.

No WSL2/Docker on this machine, so skill tests run in the
security/subprocess_sandbox.py restricted process with JARVIS-side extras:
memory cap (~4 GB) and no network egress. Promotion requires tests passing
AND the user's approval; the approval state persists to .openjarvis/.
"""
