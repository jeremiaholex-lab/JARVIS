"""JARVIS Phase 4 — auto-heal sentry (design §6.7 last paragraph).

Background watcher that catches agent runtime failures, uses the LLM to
diagnose them, and proposes a patch that is applied INSIDE THE SANDBOX
only — for agent skill code. Never silently patches the user's own files;
those go through the normal write-tier confirm.

Flow:
  failure trace (traces.db / SkillExecutor exception)
    -> LLM diagnosis + patch (local Ollama first, Agnes escalation)
    -> patch targets validated to be agent skill code only
    -> patch applied to a scratch copy, re-tested in the restricted process
    -> passing candidate parked in .openjarvis/skill-heals/<name>/<ts>/
    -> human approval gate (same as §6.6 step 3) -> promoted to live skills
"""
