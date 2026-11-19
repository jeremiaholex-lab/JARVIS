"""Morning briefing checks.

The Phase 3 done-test claims a spoken digest over email, calendar, tasks,
projects and deadlines. These tests verify the digest *content* offline with a
fake Google connector and a fake speaker; nothing here touches the network or
an audio device. Set JARVIS_LIVE_DIGEST=1 to additionally speak a real digest.
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

from jarvis.skills.morning_digest import MorningDigestAgent
from project_store import ProjectStore


class FakeGoogle:
    def __init__(self):
        self.calls = 0

    def fetch_daily(self):
        self.calls += 1
        return {
            "email": ["1 unread from Acme", "invoice due"],
            "calendar": ["10:00 standup"],
            "tasks": [],
        }


class BrokenGoogle:
    def fetch_daily(self):
        raise RuntimeError("token expired")


def _store_with(tmp_path, hours_from_now, title="Ship the digest"):
    store = ProjectStore(tmp_path / "p.db")
    pid = store.add_project("JARVIS", str(tmp_path))
    did = store.add_deadline(pid, title, (datetime.now(timezone.utc) + timedelta(hours=hours_from_now)).isoformat())
    return store, did


def test_digest_covers_all_five_sources(tmp_path):
    store, _ = _store_with(tmp_path, 6, "Renew the domain")
    google = FakeGoogle()
    agent = MorningDigestAgent(store, google=google)

    digest = asyncio.run(agent.generate_digest())

    assert "Email: 1 unread from Acme" in digest
    assert "Calendar: 10:00 standup" in digest
    assert "Tasks: nothing needing attention" in digest
    assert "Projects: JARVIS" in digest
    assert "Renew the domain" in digest
    assert digest.startswith("Good morning. Today is")


def test_digest_states_overdue_separately(tmp_path):
    store, _ = _store_with(tmp_path, -5, "Missed filing")
    digest = asyncio.run(MorningDigestAgent(store, google=None).generate_digest())
    assert "overdue" in digest
    assert "Google email, calendar, and tasks are not connected." in digest


def test_google_failure_degrades_instead_of_losing_the_briefing(tmp_path):
    store, _ = _store_with(tmp_path, 3, "Close the sprint")
    digest = asyncio.run(MorningDigestAgent(store, google=BrokenGoogle()).generate_digest())
    assert "Google data unavailable (RuntimeError)" in digest
    assert "Close the sprint" in digest, "local sections must survive a connector failure"


def test_speak_digest_uses_the_injected_speaker(tmp_path):
    spoken = []
    store, _ = _store_with(tmp_path, 4)
    agent = MorningDigestAgent(store, google=None, speaker=spoken.append)
    asyncio.run(agent.run_morning_cycle())
    assert len(spoken) == 1 and spoken[0].startswith("Good morning.")


def test_scheduler_marks_alerts_persistently(tmp_path):
    """A restart must not re-speak a reminder the owner already heard."""
    store, did = _store_with(tmp_path, 5)
    agent = MorningDigestAgent(store, google=None, speaker=lambda t: None)
    upcoming = store.get_proactive_deadlines()
    assert [d["id"] for d in upcoming] == [did]
    for d in upcoming:
        store.mark_deadline_alerted(d["id"])
    assert ProjectStore(tmp_path / "p.db").alerted_deadline_ids() == {did}


class EmojiGoogle:
    def fetch_daily(self):
        return {
            "email": ["\U0001F7E0 Urgent: invoice due", "Re: The \u201cclient\u201d replied"],
            "calendar": ["10:00 \u2014 standup"],
            "tasks": [],
        }


def test_digest_survives_emoji_and_curly_quotes(tmp_path, capsys):
    """Live crash: an emoji subject raised UnicodeEncodeError on the Windows
    cp1252 console, killing the whole morning briefing."""
    import sys

    store, _ = _store_with(tmp_path, 6)
    digest = asyncio.run(MorningDigestAgent(store, google=EmojiGoogle()).generate_digest())

    digest.encode(sys.stdout.encoding)  # must be printable here
    assert "Urgent: invoice due" in digest
    assert "\u201c" not in digest and "\U0001F7E0" not in digest
    print(digest)  # the original failure mode was the print itself
    assert capsys.readouterr().out.strip(), "the digest must actually print"


def test_plain_normalizes_typography_and_drops_unencodable_text():
    from jarvis.skills.morning_digest import _plain

    assert _plain("The \u201cclient\u201d replied \u2014 later\u2026") == 'The "client" replied - later...'
    assert _plain("caf\u00e9 na\u00efve") == "caf\u00e9 na\u00efve"  # encodable stays
    assert _plain("\U0001F7E0 hello") == "hello"
    assert _plain("   \n  multi   line \t text  ") == "multi line text"


@pytest.mark.skipif(os.environ.get("JARVIS_LIVE_DIGEST") != "1",
                    reason="set JARVIS_LIVE_DIGEST=1 to speak a real digest")
def test_live_digest_smoke():
    from jarvis.skills.morning_digest import make_agent

    agent = make_agent(ProjectStore("jarvis_projects.db"))
    asyncio.run(agent.run_morning_cycle())
