"""Executor tests: exactly one approved desktop action runs, and cancellation
beats execution.

Every test injects a fake into the executor — no test moves the real cursor,
sends real input, constructs the real Win32 boundary, or opens a real file.
The audit logger is always faked, so no test touches the real audit chain.
"""
from __future__ import annotations

import time

import pytest

from jarvis.desktop.actions import (
    DesktopAction,
    cancel_pending_desktop_actions,
    configure_desktop_audit,
    create_desktop_action_proposal,
    redact_sensitive,
    resolve_desktop_action,
)
from jarvis.desktop import executor as executor_module
from jarvis.desktop.executor import DesktopActionExecutor


class FakeDesktop:
    """Records every protocol call; the executor never touches a real desktop."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def cursor_pos(self):
        return (0, 0)

    def move_cursor(self, x, y):
        self.calls.append(("move_cursor", x, y))

    def click(self, x, y, button, clicks):
        self.calls.append(("click", x, y, button, clicks))

    def scroll(self, x, y, direction, amount):
        self.calls.append(("scroll", x, y, direction, amount))

    def type_text(self, text):
        self.calls.append(("type_text", text))

    def press_keys(self, keys):
        self.calls.append(("press_keys", list(keys)))

    def focus_window(self, hwnd):
        self.calls.append(("focus_window", hwnd))

    def window_rect(self, hwnd):
        return (0, 0, 100, 100)

    def open(self, path):
        self.calls.append(("open", path))


class BrokenDesktop(FakeDesktop):
    """Fails the moment the executor asks it to do anything."""

    def move_cursor(self, x, y):
        raise OSError("injected fault: no display attached")

    def click(self, x, y, button, clicks):
        raise OSError("injected fault: no display attached")

    def scroll(self, x, y, direction, amount):
        raise OSError("injected fault: no display attached")

    def type_text(self, text):
        raise OSError("injected fault: no display attached")

    def press_keys(self, keys):
        raise OSError("injected fault: no display attached")


@pytest.fixture(autouse=True)
def _clean_state():
    cancel_pending_desktop_actions("executor test setup")
    configure_desktop_audit(None)
    executor_module.reset_cancel()
    yield
    cancel_pending_desktop_actions("executor test teardown")
    configure_desktop_audit(None)
    executor_module.reset_cancel()


def _click_action(action_id="a1"):
    return DesktopAction(
        action_id, "click", {"x": 10, "y": 20, "button": "left", "clicks": 1},
        "write", {"description": "Click a harmless target"},
        time.time() + 60,
    )


def _snapshot(action):
    return {
        "action_id": action.action_id,
        "kind": action.kind,
        "target": dict(action.target),
        "risk": action.risk,
        "evidence": dict(action.evidence),
        "expires_at": action.expires_at,
    }


def test_stop_cancels_pending_action():
    """Step 1: a HUD stop before approval makes the later execute a no-op."""
    executor = DesktopActionExecutor(FakeDesktop())
    action = _click_action()
    create_desktop_action_proposal(action)
    executor.cancel("hud stop")
    result = executor.execute(
        action.action_id, "yes, do it", _snapshot(action)
    )
    assert result["status"] == "cancelled"


def test_new_user_turn_cancels_pending_desktop_action():
    """Step 4: the orb's new-turn hook reaches the executor's cancellation
    flag through the shared pending store."""
    from orb import server

    executor = DesktopActionExecutor(FakeDesktop())
    action = _click_action()
    create_desktop_action_proposal(action)
    server.cancel_pending_desktop_actions("new turn")
    assert executor.is_cancelled() is True


# ---------------------------------------------------------------- execution


def test_approved_click_runs_exactly_one_action():
    fake = FakeDesktop()
    executor = DesktopActionExecutor(fake)
    action = _click_action()
    snapshot = create_desktop_action_proposal(action)["snapshot"]
    result = executor.execute(action.action_id, "yes, do it", snapshot)
    assert result["status"] == "ok"
    assert fake.calls == [
        ("move_cursor", 10, 20),
        ("click", 10, 20, "left", 1),
    ]


def test_replay_of_a_spent_approval_never_reexecutes():
    """One-shot: after one success the card is gone; a stale replay or
    snapshot returns a failure and no control call happens."""
    fake = FakeDesktop()
    executor = DesktopActionExecutor(fake)
    action = _click_action()
    snapshot = create_desktop_action_proposal(action)["snapshot"]
    assert executor.execute(action.action_id, "yes, do it", snapshot)["status"] == "ok"
    assert fake.calls.count(("click", 10, 20, "left", 1)) == 1
    assert executor.execute(action.action_id, "yes, do it", snapshot)["status"] != "ok"
    assert fake.calls.count(("click", 10, 20, "left", 1)) == 1


def test_wrong_snapshot_fails_closed_without_input():
    fake = FakeDesktop()
    executor = DesktopActionExecutor(fake)
    action = _click_action()
    snapshot = create_desktop_action_proposal(action)["snapshot"]
    tampered = dict(snapshot)
    tampered["target"] = {"x": 999, "y": 999, "button": "left", "clicks": 1}
    result = executor.execute(action.action_id, "yes, do it", tampered)
    assert result["status"] != "ok"
    assert fake.calls == []


def test_unknown_action_id_fails_closed():
    executor = DesktopActionExecutor(FakeDesktop())
    result = executor.execute(
        "never-proposed", "yes, do it", _snapshot(_click_action("never-proposed"))
    )
    assert result["status"] != "ok"


def test_unknown_action_id_is_audited():
    """I-1: an id that was never proposed leaves a failed audit entry —
    a client replaying an unknown id N times must not vanish from the
    trail."""
    events: list[str] = []

    class FakeAudit:
        def log(self, event):
            events.append(event.content_preview)

    configure_desktop_audit(FakeAudit())
    try:
        executor = DesktopActionExecutor(FakeDesktop())
        result = executor.execute(
            "never-proposed", "yes, do it", _snapshot(_click_action("never-proposed"))
        )
    finally:
        configure_desktop_audit(None)

    assert result["status"] == "failed"
    kinds = _audit_kinds(events)
    assert kinds and kinds[-1] == "failed", kinds


def test_spent_action_id_replay_is_audited():
    """I-1: after a card is consumed by a success, replaying the same id
    leaves a failed audit entry (not just a silent failure)."""
    events: list[str] = []

    class FakeAudit:
        def log(self, event):
            events.append(event.content_preview)

    configure_desktop_audit(FakeAudit())
    try:
        fake = FakeDesktop()
        executor = DesktopActionExecutor(fake)
        action = _click_action()
        snapshot = create_desktop_action_proposal(action)["snapshot"]
        assert executor.execute(action.action_id, "yes, do it", snapshot)["status"] == "ok"
        replay = executor.execute(action.action_id, "yes, do it", snapshot)
    finally:
        configure_desktop_audit(None)

    assert replay["status"] == "failed"
    kinds = _audit_kinds(events)
    # The success was audited; the replay failure must be too.
    assert kinds[-1] == "failed", kinds
    assert fake.calls.count(("click", 10, 20, "left", 1)) == 1


def test_execution_failure_is_reported_and_never_retryable():
    fake = BrokenDesktop()
    executor = DesktopActionExecutor(fake)
    action = _click_action()
    snapshot = create_desktop_action_proposal(action)["snapshot"]
    result = executor.execute(action.action_id, "yes, do it", snapshot)
    assert result["status"] == "failed"
    assert "injected fault" in result.get("error", "")
    # The card is spent either way: a retry cannot re-execute it.
    assert executor.execute(action.action_id, "yes, do it", snapshot)["status"] != "ok"


def test_type_action_must_be_typed_to_proceed():
    """The typed phrase is the only door into execution. A bad phrase on a
    card consumes it (Task 1 one-shot), so each attempt gets its own card."""
    fake = FakeDesktop()
    executor = DesktopActionExecutor(fake)
    first = DesktopAction("t1", "type", {"text": "hello"}, "write", {}, time.time() + 60)
    first_snapshot = create_desktop_action_proposal(first)["snapshot"]
    assert executor.execute(first.action_id, "yes", first_snapshot)["status"] != "ok"
    assert fake.calls == []

    second = DesktopAction("t2", "type", {"text": "hello"}, "write", {}, time.time() + 60)
    second_snapshot = create_desktop_action_proposal(second)["snapshot"]
    assert executor.execute(second.action_id, "yes, do it", second_snapshot)["status"] == "ok"
    assert fake.calls == [("type_text", "hello")]


def test_press_keys_action_normalizes_keycodes():
    fake = FakeDesktop()
    executor = DesktopActionExecutor(fake)
    action = DesktopAction(
        "k1", "press_keys", {"keys": [48, "enter"]}, "write", {}, time.time() + 60
    )
    snapshot = create_desktop_action_proposal(action)["snapshot"]
    assert executor.execute(action.action_id, "yes, do it", snapshot)["status"] == "ok"
    assert fake.calls[-1] == ("press_keys", ["48", "enter"])


# ------------------------------------------------------------ launch policy


def test_launch_policy_revalidated_at_execution_time():
    """TOCTOU closure: approval does not freeze the policy. A target that
    now fails validate_launch_target must fail closed, before any OS effect."""
    from jarvis.desktop.targets import validate_launch_target

    path = "C:\\Tools\\tool.exe"
    allowed, why = validate_launch_target(path)
    assert allowed is False
    assert why  # the rejection must explain itself without echoing the target

    fake = FakeDesktop()
    executor = DesktopActionExecutor(fake)
    action = DesktopAction(
        "l1", "open_app", {"name_or_path": path}, "write", {}, time.time() + 60
    )
    snapshot = create_desktop_action_proposal(action)["snapshot"]
    result = executor.execute(action.action_id, "yes, do it", snapshot)
    assert result["status"] == "failed"
    assert fake.calls == []


def test_launch_policy_rejected_reason_never_echoes_a_secret():
    fake = FakeDesktop()
    executor = DesktopActionExecutor(fake)
    path = "C:\\Secrets\\password.txt"
    action = DesktopAction(
        "l2", "open_file", {"path": path}, "write", {}, time.time() + 60
    )
    snapshot = create_desktop_action_proposal(action)["snapshot"]
    result = executor.execute(action.action_id, "yes, do it", snapshot)
    assert result["status"] == "failed"
    assert path not in result.get("error", "")
    assert fake.calls == []


def test_allowed_launch_reaches_the_open_control():
    """A document target that passes revalidation executes through the
    control's open hook, exactly once."""
    fake = FakeDesktop()
    executor = DesktopActionExecutor(fake)
    action = DesktopAction(
        "l3", "open_file", {"path": "C:\\Reports\\budget.docx"},
        "write", {}, time.time() + 60,
    )
    snapshot = create_desktop_action_proposal(action)["snapshot"]
    assert executor.execute(action.action_id, "yes, do it", snapshot)["status"] == "ok"
    assert fake.calls == [("open", "C:\\Reports\\budget.docx")]


# --------------------------------------------------------------------- audit


def _audit_kinds(events):
    return [s.split(" event=")[1].split(" ")[0] for s in events]


def test_executor_transitions_are_audited():
    events: list[str] = []

    class FakeAudit:
        def log(self, event):
            events.append(event.content_preview)

    configure_desktop_audit(FakeAudit())
    try:
        executor = DesktopActionExecutor(FakeDesktop())
        action = _click_action()
        snapshot = create_desktop_action_proposal(action)["snapshot"]
        assert executor.execute(action.action_id, "yes, do it", snapshot)["status"] == "ok"
    finally:
        configure_desktop_audit(None)

    kinds = _audit_kinds(events)
    assert "executed" in kinds
    assert kinds[-1] == "executed"


def test_cancellation_is_audited():
    events: list[str] = []

    class FakeAudit:
        def log(self, event):
            events.append(event.content_preview)

    configure_desktop_audit(FakeAudit())
    try:
        executor = DesktopActionExecutor(FakeDesktop())
        action = _click_action()
        create_desktop_action_proposal(action)
        executor.cancel("hud stop")
        result = executor.execute(action.action_id, "yes, do it", _snapshot(action))
    finally:
        configure_desktop_audit(None)

    assert result["status"] == "cancelled"
    kinds = _audit_kinds(events)
    assert "cancelled" in kinds
    assert kinds[-1] == "cancelled"


def test_failure_reason_is_redacted_before_it_is_audited():
    """Error text flows through the shared redaction policy: a fault that
    names a sensitive window can never reach the audit trail verbatim."""
    assert redact_sensitive("error: 1Password vault closed") == "[REDACTED]"


def test_reset_cancel_allows_a_new_request_to_proceed():
    """The latch is process-wide: a host clears it when a new request begins,
    and only the shared pending store (Task 1) decides what can run."""
    executor = DesktopActionExecutor(FakeDesktop())
    action = _click_action()
    snapshot = create_desktop_action_proposal(action)["snapshot"]
    executor.cancel("hud stop")
    assert executor.is_cancelled() is True
    executor_module.reset_cancel()
    assert executor.is_cancelled() is False
    # The spent card still cannot run: one action per user request.
    assert executor.execute(action.action_id, "yes, do it", snapshot)["status"] != "ok"
