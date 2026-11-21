from __future__ import annotations

from jarvis.security.auditor import Auditor, Decision
from jarvis.security.tiers import PlannedAction


class FakeEngine:
    """Engine that returns a fixed critic response."""

    def __init__(self, content: str):
        self.content = content

    def generate(self, messages, model=None, max_tokens=None, temperature=0.0):
        return {"content": self.content}


class BrokenEngine:
    """Engine that fails at the exact boundary the critic must survive."""

    def generate(self, messages, model=None, max_tokens=None, temperature=0.0):
        raise RuntimeError("model unavailable")


def _write_action() -> PlannedAction:
    return PlannedAction(tool="file_write", params={"path": "notes.txt", "content": "hello"})


def test_unreadable_llm_critic_response_holds_the_action():
    verdict = Auditor(local_engine=FakeEngine("garbage")).judge(_write_action())
    assert verdict.decision is Decision.HOLD


def test_empty_llm_critic_response_holds_the_action():
    verdict = Auditor(local_engine=FakeEngine("")).judge(_write_action())
    assert verdict.decision is Decision.HOLD


def test_llm_critic_engine_failure_holds_the_action():
    verdict = Auditor(local_engine=BrokenEngine()).judge(_write_action())
    assert verdict.decision is Decision.HOLD


def test_explicit_critic_pass_is_still_allowed():
    verdict = Auditor(local_engine=FakeEngine(
        '{"decision": "pass", "reason": "safe"}'
    )).judge(_write_action())
    assert verdict.decision is Decision.PASS
