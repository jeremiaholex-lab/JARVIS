from datetime import datetime, timedelta, timezone

from project_store import ProjectStore


def _make(store, due_iso, title="Task", status="open"):
    pid = store.add_project("P", ".")
    did = store.add_deadline(pid, title, due_iso)
    if status != "open":
        store.update_deadline_status(did, status)
    return did


def test_overdue_deadlines_are_surfaced_not_dropped(tmp_path):
    store = ProjectStore(tmp_path / "p.db")
    now = datetime.now(timezone.utc)
    overdue = _make(store, (now - timedelta(hours=5)).isoformat(), "Missed invoice")

    due = store.get_proactive_deadlines(now)
    assert [d["id"] for d in due] == [overdue], "overdue work must stay in the proactive window"


def test_window_is_48_hours_and_excludes_the_distant_future(tmp_path):
    store = ProjectStore(tmp_path / "p.db")
    now = datetime.now(timezone.utc)
    soon = _make(store, (now + timedelta(hours=6)).isoformat(), "Soon")
    _make(store, (now + timedelta(days=30)).isoformat(), "Later")

    assert [d["id"] for d in store.get_proactive_deadlines(now)] == [soon]


def test_completed_deadlines_leave_the_window(tmp_path):
    store = ProjectStore(tmp_path / "p.db")
    now = datetime.now(timezone.utc)
    _make(store, (now + timedelta(hours=2)).isoformat(), "Done thing", "completed")
    assert store.get_proactive_deadlines(now) == []


def test_in_memory_store_keeps_its_schema(tmp_path):
    """A fresh connection per operation would lose :memory: state entirely."""
    store = ProjectStore(":memory:")
    pid = store.add_project("P", str(tmp_path))
    store.add_deadline(pid, "Kept", (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat())
    assert store.list_projects()
    assert len(store.get_proactive_deadlines()) == 1
    store.close()


def test_alerts_survive_a_reopen_of_the_worker(tmp_path):
    """The old worker kept alert state in a set, so every restart re-spoke it."""
    db = tmp_path / "p.db"
    store = ProjectStore(db)
    now = datetime.now(timezone.utc)
    did = _make(store, (now + timedelta(hours=3)).isoformat(), "Site visit")

    assert store.alerted_deadline_ids() == set()
    store.mark_deadline_alerted(did)
    assert ProjectStore(db).alerted_deadline_ids() == {did}


def test_alert_resets_when_status_changes(tmp_path):
    store = ProjectStore(tmp_path / "p.db")
    now = datetime.now(timezone.utc)
    did = _make(store, (now + timedelta(hours=3)).isoformat(), "Site visit")
    store.mark_deadline_alerted(did)

    store.update_deadline_status(did, "completed")
    assert store.alerted_deadline_ids() == set()

    store.update_deadline_status(did, "open")
    assert store.alerted_deadline_ids() == set(), "reopened deadline should remind again"
