"""Tests for the cron schedule layer (design: 2026-11-20-cron-schedule-layer).

Plain-pytest style matching test_task_runner.py: isolated tmp_path SQLite DBs,
explicit time via the ``check(now=...)`` seam so nothing sleeps.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from jarvis.agent.schedules import (
    CronError, MAX_SCHEDULES, ScheduleStore, next_slot, parse_cron,
)
from jarvis.agent.task_runner import TaskStore


def _db(tmp_path):
    return tmp_path / "tasks.db"


def _make_stores(tmp_path):
    db = _db(tmp_path)
    tasks = TaskStore(db)
    return db, tasks, ScheduleStore(db, tasks)


def _set_next_run(db_path, schedule_id, value):
    db = sqlite3.connect(str(db_path))
    db.execute("UPDATE agent_schedules SET next_run_at=? WHERE id=?", (value, schedule_id))
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def test_parse_basic_fields():
    p = parse_cron("*/15 0-5 1,15 3 1-5")
    assert p["minute"] == frozenset({0, 15, 30, 45})
    assert p["hour"] == frozenset(range(6))
    assert p["day_of_month"] == frozenset({1, 15})
    assert p["month"] == frozenset({3})
    assert p["day_of_week"] == frozenset({1, 2, 3, 4, 5})
    assert p["dom_restricted"] is True
    assert p["dow_restricted"] is True


def test_sunday_normalizes_7_to_0():
    assert parse_cron("0 0 * * 7")["day_of_week"] == frozenset({0})
    assert parse_cron("0 0 * * 0")["day_of_week"] == frozenset({0})


def test_star_means_full_range():
    p = parse_cron("* * * * *")
    assert p["minute"] == frozenset(range(60))
    assert p["hour"] == frozenset(range(24))
    assert p["dom_restricted"] is False
    assert p["dow_restricted"] is False


def test_reject_wrong_field_count():
    with pytest.raises(CronError):
        parse_cron("0 9 * * 1 1")  # six fields
    with pytest.raises(CronError):
        parse_cron("0 9 * *")  # four fields


def test_reject_names_and_specials():
    # JAN / L / W are outside the restricted grammar; "1 * *" is valid and must NOT raise.
    for expr in ("0 9 * JAN *", "0 9 L * *", "0 9 1W * *"):
        with pytest.raises(CronError):
            parse_cron(expr)
    parse_cron("0 9 1 * *")  # valid: dom=1, month *, dow *


def test_reject_out_of_range():
    with pytest.raises(CronError):
        parse_cron("61 * * * *")  # minute 61
    with pytest.raises(CronError):
        parse_cron("0 24 * * *")  # hour 24


def test_reject_inverted_range():
    with pytest.raises(CronError):
        parse_cron("0 9 * * 5-1")


def test_reject_step_without_range_or_star():
    with pytest.raises(CronError):
        parse_cron("5/2 * * * *")  # step on a bare number


def test_step_on_range():
    assert parse_cron("0-30/10 * * * *")["minute"] == frozenset({0, 10, 20, 30})


# ---------------------------------------------------------------------------
# next_slot
# ---------------------------------------------------------------------------

def test_next_slot_every_15min():
    import datetime as dt
    ts = time.time()
    ns = next_slot("*/15 * * * *", ts)
    assert ns > ts
    assert dt.datetime.fromtimestamp(ns).minute in {0, 15, 30, 45}


def test_next_slot_daily_noon():
    import datetime as dt
    ts = time.time()
    ns = next_slot("0 12 * * *", ts)
    c = dt.datetime.fromtimestamp(ns)
    assert ns > ts and c.hour == 12 and c.minute == 0


def test_next_slot_impossible_returns_zero():
    # February 30 never exists; both dom and dow restricted -> AND, no match.
    assert next_slot("0 9 30 2 *", time.time()) == 0.0


def test_next_slot_sunday_equivalent():
    ts = time.time()
    assert next_slot("0 0 * * 0", ts) == next_slot("0 0 * * 7", ts)


def test_next_slot_accepts_parsed_spec():
    spec = parse_cron("0 12 * * *")
    ts = time.time()
    assert next_slot(spec, ts) == next_slot("0 12 * * *", ts)


# ---------------------------------------------------------------------------
# store CRUD
# ---------------------------------------------------------------------------

def test_store_crud(tmp_path):
    _, _, scheds = _make_stores(tmp_path)
    s = scheds.create("* * * * *", "summarize last week's emails")
    assert s.enabled is True and s.next_run_at > 0
    assert scheds.get(s.id).instruction == "summarize last week's emails"
    assert len(scheds.list()) == 1
    assert scheds.set_enabled(s.id, False).enabled is False
    resumed = scheds.set_enabled(s.id, True)
    assert resumed.enabled is True and resumed.next_run_at > 0
    scheds.remove(s.id)
    assert scheds.get(s.id) is None


def test_create_rejects_impossible(tmp_path):
    _, _, scheds = _make_stores(tmp_path)
    with pytest.raises(CronError):
        scheds.create("0 9 30 2 *", "never")  # Feb 30


def test_create_rejects_empty_instruction(tmp_path):
    _, _, scheds = _make_stores(tmp_path)
    with pytest.raises(ValueError):
        scheds.create("* * * * *", "   ")


def test_schedule_cap(tmp_path):
    _, _, scheds = _make_stores(tmp_path)
    for i in range(MAX_SCHEDULES):
        scheds.create("* * * * *", f"task {i}")
    with pytest.raises(ValueError):
        scheds.create("* * * * *", "one too many")


def test_remove_unknown_raises(tmp_path):
    _, _, scheds = _make_stores(tmp_path)
    with pytest.raises(KeyError):
        scheds.remove("nope")


# ---------------------------------------------------------------------------
# catch-up + idempotency
# ---------------------------------------------------------------------------

def test_catchup_fires_once_and_resyncs(tmp_path):
    db, tasks, scheds = _make_stores(tmp_path)
    s = scheds.create("* * * * *", "tick")  # every minute
    now = time.time()
    past_due = now - 180  # three slots missed
    _set_next_run(db, s.id, past_due)

    fired = scheds.check(now=now)
    assert len(fired) == 1
    assert fired[0]["task"] is not None
    assert fired[0]["task"].idempotency_key == f"schedule:{s.id}:{int(past_due)}"

    # Resynced to a future slot; a check just before it fires nothing.
    stored = scheds.get(s.id)
    assert stored.next_run_at > now
    assert scheds.check(now=stored.next_run_at - 0.1) == []


def test_idempotency_suppresses_duplicate(tmp_path):
    db, tasks, scheds = _make_stores(tmp_path)
    s = scheds.create("* * * * *", "tick")
    now = time.time()
    past_due = now - 60
    _set_next_run(db, s.id, past_due)
    # A prior run already enqueued this exact slot before it could bookkeep.
    tasks.create("[prior] tick", idempotency_key=f"schedule:{s.id}:{int(past_due)}")

    fired = scheds.check(now=now)
    assert len(fired) == 1
    # TaskStore.create with a colliding idempotency key returns the existing
    # row (no duplicate queued). The fired record points at that prior task.
    assert fired[0]["task"] is not None
    assert fired[0]["task"].idempotency_key == f"schedule:{s.id}:{int(past_due)}"
    assert fired[0]["task"].goal == "[prior] tick"  # it is the earlier enqueue, not a new one


def test_fired_task_is_claimable(tmp_path):
    db, tasks, scheds = _make_stores(tmp_path)
    s = scheds.create("* * * * *", "do the thing")
    now = time.time()
    past_due = now - 60
    _set_next_run(db, s.id, past_due)

    fired = scheds.check(now=now)
    task = fired[0]["task"]
    assert task is not None
    assert task.goal.startswith("[schedule ")
    assert task.idempotency_key == f"schedule:{s.id}:{int(past_due)}"
    claimed = tasks.claim_next(max_attempts=3)
    assert claimed.id == task.id
