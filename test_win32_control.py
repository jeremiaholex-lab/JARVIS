"""Fake-boundary tests for the Win32 desktop adapter.

No test here is allowed to touch the real cursor, keyboard, or window stack.
``Win32DesktopControl(os=fake)`` routes every OS effect into ``FakeDesktop``,
which records semantic calls for assertions.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

import pytest

from jarvis.desktop.win32_control import DesktopControl, Win32DesktopControl


class FakeDesktop:
    """Deterministic stand-in for every Win32 call the adapter may make."""

    def __init__(
        self,
        cursor_pos: tuple[int, int] = (10, 20),
        window_rect: tuple[int, int, int, int] = (0, 0, 100, 100),
        foreground_hwnd: int = 7,
        dpi: int = 96,
    ) -> None:
        self.calls: list[tuple] = []
        self.dpi_reads: list[int] = []
        self.cursor = cursor_pos
        self.window_rect_value = window_rect
        self.foreground_hwnd = foreground_hwnd
        self.dpi = dpi

    def get_cursor_pos(self):
        self.calls.append(("get_cursor_pos",))
        return self.cursor

    def set_cursor_pos(self, x: int, y: int):
        self.calls.append(("set_cursor_pos", x, y))
        self.cursor = (x, y)

    def click(self, x: int, y: int, button: str, clicks: int):
        self.calls.append(("click", x, y, button, clicks))

    def scroll(self, x: int, y: int, direction: str, amount: int):
        self.calls.append(("scroll", x, y, direction, amount))

    def type_text(self, text: str):
        self.calls.append(("type_text", text))

    def press_keys(self, keys: list[str]):
        self.calls.append(("press_keys", list(keys)))

    def get_foreground_window(self):
        self.calls.append(("get_foreground_window",))
        return self.foreground_hwnd

    def set_foreground_window(self, hwnd: int):
        self.calls.append(("set_foreground_window", hwnd))
        self.foreground_hwnd = hwnd

    def get_window_rect(self, hwnd: int):
        self.calls.append(("get_window_rect", hwnd))
        return self.window_rect_value

    def get_dpi(self):
        self.dpi_reads.append(self.dpi)
        return self.dpi


def test_click_uses_approved_coordinates():
    fake = FakeDesktop()
    control = Win32DesktopControl(os=fake)
    control.click(42, 99, "left", 2)
    assert fake.calls[-1] == ("click", 42, 99, "left", 2)


def test_window_rect_is_returned():
    fake = FakeDesktop(window_rect=(1, 2, 300, 400))
    assert Win32DesktopControl(os=fake).window_rect(7) == (1, 2, 300, 400)


def test_cursor_pos_comes_from_the_fake_boundary():
    fake = FakeDesktop(cursor_pos=(11, 22))
    assert Win32DesktopControl(os=fake).cursor_pos() == (11, 22)
    assert fake.calls == [("get_cursor_pos",)]


def test_move_cursor_uses_the_approved_target():
    fake = FakeDesktop()
    Win32DesktopControl(os=fake).move_cursor(33, 44)
    assert fake.calls == [("set_cursor_pos", 33, 44)]


def test_scroll_preserves_direction_and_amount():
    fake = FakeDesktop()
    Win32DesktopControl(os=fake).scroll(5, 6, "down", 3)
    assert fake.calls == [("scroll", 5, 6, "down", 3)]


def test_type_and_press_keys_go_through_the_boundary():
    fake = FakeDesktop()
    control = Win32DesktopControl(os=fake)
    control.type_text("hello")
    control.press_keys(["ctrl", "a"])
    assert fake.calls == [("type_text", "hello"), ("press_keys", ["ctrl", "a"])]


def test_focus_window_uses_the_fake_window():
    fake = FakeDesktop(foreground_hwnd=1)
    control = Win32DesktopControl(os=fake)
    control.focus_window(7)
    assert fake.calls == [("set_foreground_window", 7)]


def test_dpi_conversion_is_confined_to_the_adapter():
    fake = FakeDesktop(cursor_pos=(100, 200), window_rect=(10, 20, 30, 40), dpi=192)
    control = Win32DesktopControl(os=fake)
    assert control.cursor_pos() == (50, 100)
    assert control.window_rect(7) == (5, 10, 15, 20)
    control.click(100, 200, "left", 1)
    assert fake.calls[-1] == ("click", 200, 400, "left", 1)
    assert fake.dpi_reads == [192, 192, 192]


def test_default_dpi_keeps_logical_coordinates_unchanged():
    fake = FakeDesktop()
    control = Win32DesktopControl(os=fake)
    control.move_cursor(81, 82)
    assert fake.calls[-1] == ("set_cursor_pos", 81, 82)


def test_unsupported_button_fails_closed():
    fake = FakeDesktop()
    with pytest.raises(ValueError):
        Win32DesktopControl(os=fake).click(1, 2, "middle", 1)
    assert fake.calls == []


def test_unsupported_scroll_direction_fails_closed():
    fake = FakeDesktop()
    with pytest.raises(ValueError):
        Win32DesktopControl(os=fake).scroll(1, 2, "sideways", 1)
    assert fake.calls == []


def test_unsupported_key_fails_closed():
    fake = FakeDesktop()
    with pytest.raises(ValueError):
        Win32DesktopControl(os=fake).press_keys(["ctrl", "escape-all"])
    assert fake.calls == []


def test_escape_is_not_intercepted_by_press_keys():
    """Escape stays a normal key press; no global hotkey is installed here."""
    fake = FakeDesktop()
    Win32DesktopControl(os=fake).press_keys(["escape"])
    assert fake.calls == [("press_keys", ["escape"])]


@runtime_checkable
class _ExpectedControl(Protocol):
    def cursor_pos(self) -> tuple[int, int]: ...
    def move_cursor(self, x: int, y: int) -> None: ...
    def click(self, x: int, y: int, button: str, clicks: int) -> None: ...
    def scroll(self, x: int, y: int, direction: str, amount: int) -> None: ...
    def type_text(self, text: str) -> None: ...
    def press_keys(self, keys: list[str]) -> None: ...
    def focus_window(self, hwnd: int) -> None: ...
    def window_rect(self, hwnd: int) -> tuple[int, int, int, int]: ...


def test_public_protocol_matches_downstream_contract():
    assert isinstance(Win32DesktopControl(os=FakeDesktop()), _ExpectedControl)
    assert isinstance(DesktopControl, type)  # runtime_checkable Protocol
    for name in (
        "cursor_pos", "move_cursor", "click", "scroll", "type_text",
        "press_keys", "focus_window", "window_rect",
    ):
        assert hasattr(DesktopControl, name)
