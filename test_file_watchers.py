from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.agent.file_watchers import FileWatcherManager


class TaskQueue:
    def __init__(self):
        self.items = {}

    def create(self, goal, *, idempotency_key="", watch_root=""):
        if idempotency_key in self.items:
            return self.items[idempotency_key]
        task = SimpleNamespace(id=f"task-{len(self.items) + 1}", goal=goal, watch_root=watch_root)
        self.items[idempotency_key] = task
        return task


def test_watcher_baselines_then_debounces_new_and_modified_files(tmp_path):
    root = tmp_path / "approved"
    root.mkdir()
    existing = root / "old.md"
    existing.write_text("already here", encoding="utf-8")
    queue = TaskQueue()
    manager = FileWatcherManager(tmp_path / "watchers.db", queue.create,
                                 approved_roots=[root], debounce_seconds=2)
    watcher = manager.create(str(root), "**/*.md", "Summarize the note")
    manager.scan_interval_seconds = 0  # deterministic clock for debounce assertions

    assert manager.poll_once(now=10) == []
    assert queue.items == {}

    existing.write_text("updated", encoding="utf-8")
    assert manager.poll_once(now=20) == []  # debounce starts
    assert manager.poll_once(now=21.9) == []
    fired = manager.poll_once(now=22)
    assert len(fired) == 1
    assert fired[0]["watcher_id"] == watcher.id
    task = next(iter(queue.items.values()))
    assert task.watch_root == str(root.resolve())
    assert str(existing) in task.goal
    assert "untrusted data" in task.goal
    assert "read-only" in task.goal
    assert manager.poll_once(now=30) == []

    new_file = root / "nested" / "new.md"
    new_file.parent.mkdir()
    new_file.write_text("new note", encoding="utf-8")
    assert manager.poll_once(now=40) == []
    assert manager.poll_once(now=41) == []
    assert len(manager.poll_once(now=42)) == 1
    assert len(queue.items) == 2


def test_pause_resume_remove_and_persistence(tmp_path):
    root = tmp_path / "approved"
    root.mkdir()
    db = tmp_path / "watchers.db"
    queue = TaskQueue()
    manager = FileWatcherManager(db, queue.create, approved_roots=[root])
    watcher = manager.create(str(root), "*.txt", "Read the note")

    restored = FileWatcherManager(db, queue.create, approved_roots=[root])
    assert restored.get(watcher.id).enabled is True
    assert restored.set_enabled(watcher.id, False).enabled is False
    assert restored.set_enabled(watcher.id, True).enabled is True
    restored.remove(watcher.id)
    assert restored.get(watcher.id) is None
    with pytest.raises(KeyError, match="not found"):
        restored.remove(watcher.id)


def test_watcher_rejects_roots_outside_allowlist_and_unsafe_patterns(tmp_path):
    approved = tmp_path / "approved"
    approved.mkdir()
    outside = tmp_path / "private"
    outside.mkdir()
    manager = FileWatcherManager(tmp_path / "watchers.db", TaskQueue().create,
                                 approved_roots=[approved])
    with pytest.raises(ValueError, match="outside approved"):
        manager.create(str(outside), "*.txt", "summarize")
    for pattern in ("", "../**/*", "/etc/*"):
        with pytest.raises(ValueError):
            manager.create(str(approved), pattern, "summarize")


def test_watcher_ignores_symlinked_files_and_external_root_changes(tmp_path):
    root = tmp_path / "approved"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("private", encoding="utf-8")
    link = root / "escape.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")

    queue = TaskQueue()
    manager = FileWatcherManager(tmp_path / "watchers.db", queue.create,
                                 approved_roots=[root], debounce_seconds=1)
    manager.create(str(root), "*.md", "summarize")
    outside.write_text("changed private data", encoding="utf-8")
    assert manager.poll_once(now=10) == []
    assert manager.poll_once(now=20) == []
    assert queue.items == {}


def test_watched_file_tools_are_confined_and_read_only(tmp_path):
    from jarvis.agent.file_watchers import build_watched_file_tools

    root = tmp_path / "watch"
    root.mkdir()
    allowed = root / "note.md"
    allowed.write_text("hello", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("private", encoding="utf-8")
    tools = {tool.name: tool for tool in build_watched_file_tools(root)}
    assert set(tools) == {"read_file", "list_dir"}
    assert "hello" in tools["read_file"].fn("note.md")
    assert "outside the watched folder" in tools["read_file"].fn(str(outside))
    assert tools["read_file"].tier.value == "read"


def test_file_watch_tool_is_read_tier_and_requires_host_configuration():
    from jarvis.agent.tools import _watch_files, configure_file_watchers
    from jarvis.security.tiers import PlannedAction, Tier, classify

    configure_file_watchers(None)
    assert 'not available' in _watch_files("list")
    assert classify(PlannedAction("jarvis_file_watch", {"action": "create"})) is Tier.READ
