import time
from datetime import datetime, timedelta, timezone

import pytest

from jarvis.proactive.alerts import Alert, AlertStore, Signal
from jarvis.proactive.triggers import (
    TriggerContext, agent_failure_trigger, collect, deadlines_trigger,
    inbox_trigger, meetings_trigger,
)

TZ = timezone(timedelta(hours=-5))


def _now():
    return datetime(2026, 11, 12, 12, 0, tzinfo=TZ)


class FakeStore:
    def __init__(self, rows):
        self.rows = rows

    def get_proactive_deadlines(self, now=None):
        return self.rows


class FakeGoogle:
    def __init__(self, data=None, raises=False):
        self.data = data or {"email": [], "calendar": [], "tasks": []}
        self.raises = raises

    def fetch_daily(self):
        if self.raises:
            raise RuntimeError("token expired")
        return self.data


# ---------------------------------------------------------------- alert store

def test_signal_validation_rejects_nonsense():
    with pytest.raises(ValueError):
        Signal(key="k", source="s", severity="catastrophic", text="t")
    with pytest.raises(ValueError):
        Signal(key="", source="s", severity="info", text="t")
    with pytest.raises(ValueError):
        Signal(key="k", source="s", severity="info", text="")


def test_repeated_signal_is_counted_not_re_announced(tmp_path):
    store = AlertStore(tmp_path / "alerts.db")
    signal = Signal(key="deadline:1:due", source="deadlines", severity="notice",
                    text="Report due in 3h", cooldown_s=3600)
    first, new = store.raise_signal(signal)
    assert new is True
    second, new = store.raise_signal(signal)
    assert new is False, "inside the cooldown this must not interrupt again"
    assert second.count == 2
    assert store.stats()["open"] == 1


def test_acknowledged_signal_can_become_fresh_again(tmp_path):
    store = AlertStore(tmp_path / "alerts.db")
    signal = Signal(key="inbox:backlog:x", source="inbox", severity="notice",
                    text="12 messages", cooldown_s=3600)
    store.raise_signal(signal)
    assert store.acknowledge("inbox:backlog:x") is True
    _, fresh = store.raise_signal(signal)
    assert fresh is True, "a re-occurrence after acknowledgement is news again"
    assert store.acknowledge("inbox:backlog:x") is False
    assert store.acknowledge("nope") is False


def test_open_alerts_sort_urgent_first(tmp_path):
    store = AlertStore(tmp_path / "alerts.db")
    store.raise_signal(Signal(key="a", source="s", severity="info", text="mild"))
    store.raise_signal(Signal(key="b", source="s", severity="urgent", text="fire"))
    store.raise_signal(Signal(key="c", source="s", severity="notice", text="medium"))
    assert [a.severity for a in store.open_alerts()] == ["urgent", "notice", "info"]
    store.acknowledge("b")
    assert [a.key for a in store.open_alerts()] == ["a", "c"] or set(
        a.key for a in store.open_alerts()) == {"a", "c"}


def test_alerts_survive_reopening(tmp_path):
    db = tmp_path / "alerts.db"
    store = AlertStore(db)
    store.raise_signal(Signal(key="k", source="s", severity="notice", text="hello"))
    reopened = AlertStore(db)
    assert reopened.open_alerts()[0].text == "hello"


# ------------------------------------------------------------------- triggers

def test_deadline_trigger_flags_overdue_and_upcoming():
    rows = [
        {"id": 1, "title": "Missed", "project_name": "P", "due_date": "2026-11-10T12:00:00-05:00"},
        {"id": 2, "title": "Soon", "project_name": "Q", "due_date": "2026-11-12T14:00:00-05:00"},
        {"id": 3, "title": "Broken", "project_name": "Q", "due_date": "not-a-date"},
    ]
    ctx = TriggerContext(project_store=FakeStore(rows), now=_now)
    signals = deadlines_trigger(ctx)
    keys = {s.key for s in signals}
    assert keys == {"deadline:1:overdue", "deadline:2:due"}
    overdue = next(s for s in signals if s.key.endswith("overdue"))
    assert "overdue by" in overdue.text
    assert overdue.severity == "urgent"
    upcoming = next(s for s in signals if s.key.endswith(":due"))
    assert "due in 2h" in upcoming.text


def test_deadline_trigger_without_a_store_is_silent():
    assert deadlines_trigger(TriggerContext()) == []


def test_inbox_trigger_requires_a_real_backlog_of_people_mail():
    spam = [f"Promo {i} <news@marketing.com>: buy" for i in range(30)]
    assert inbox_trigger(TriggerContext(google=FakeGoogle({"email": spam}))) == []

    people = [f"Person {i} <p{i}@acme.com>: question" for i in range(15)]
    signals = inbox_trigger(TriggerContext(google=FakeGoogle({"email": people})))
    assert len(signals) == 1
    assert "15 messages" in signals[0].text
    assert signals[0].suggested_action


def test_inbox_trigger_swallows_connector_failure():
    ctx = TriggerContext(google=FakeGoogle(raises=True))
    assert inbox_trigger(ctx) == []
    assert meetings_trigger(ctx) == []


def test_meeting_trigger_only_fires_inside_the_window():
    data = {"calendar": [
        "2026-11-12T12:30:00-05:00: Client call",
        "2026-11-12T18:00:00-05:00: Evening thing",
        "nonsense row without a timestamp",
    ]}
    signals = meetings_trigger(TriggerContext(google=FakeGoogle(data), now=_now))
    assert len(signals) == 1
    assert "Client call" in signals[0].text
    assert "in 30 min" in signals[0].text


def test_agent_failure_trigger_counts_real_failures(tmp_path):
    import sqlite3

    db = tmp_path / "traces.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE traces (trace_id TEXT, agent TEXT, result TEXT, outcome TEXT,"
        " started_at REAL, total_latency_seconds REAL)")
    recent = time.time() - 60
    stale = time.time() - 86400
    for trace_id, outcome, result, at in [
        ("t1", "failed", "boom", recent), ("t2", "success", "Traceback in run", recent),
        ("t3", "success", "fine", recent), ("t4", "failed", "boom", stale),
    ]:
        conn.execute("INSERT INTO traces VALUES (?,?,?,?,?,?)",
                     (trace_id, "native_openhands", result, outcome, at, 1.0))
    conn.commit()
    conn.close()

    signals = agent_failure_trigger(TriggerContext(traces_db=db))
    assert len(signals) == 1
    assert signals[0].text.startswith("2 failed run(s)")
    assert agent_failure_trigger(TriggerContext(traces_db=tmp_path / "missing.db")) == []


def test_collect_isolates_a_broken_trigger():
    class Exploding:
        def fetch_daily(self):
            raise ZeroDivisionError("source on fire")

    ctx = TriggerContext(
        google=Exploding(),
        project_store=FakeStore([{"id": 9, "title": "Keep me", "project_name": "P",
                                  "due_date": "2026-11-12T13:00:00-05:00"}]),
        now=_now,
    )
    signals = collect(ctx)
    assert any(s.source == "deadlines" for s in signals), "one bad source must not mute the rest"


def test_collect_can_run_selected_triggers_only():
    ctx = TriggerContext(
        project_store=FakeStore([{"id": 1, "title": "A", "project_name": "P",
                                  "due_date": "2026-11-12T13:00:00-05:00"}]),
        now=_now,
    )
    assert all(s.source == "deadlines" for s in collect(ctx, only=["deadlines"]))
    assert collect(ctx, only=["inbox"]) == []


def test_signals_never_carry_execution_power():
    """§6.9: raise an issue; acting is still tier-gated elsewhere."""
    import inspect

    import jarvis.proactive.triggers as module
    import jarvis.proactive.alerts as alerts_module

    src = inspect.getsource(module) + inspect.getsource(alerts_module)
    for forbidden in ("shell_exec", "file_write", "subprocess", "ToolRegistry", "http_post"):
        assert forbidden not in src, f"{forbidden} must not appear in the proactive layer"
