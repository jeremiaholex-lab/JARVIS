"""Win32 desktop control boundary.

Every OS effect lives behind the injected ``os`` object. ``Win32DesktopControl``
only validates inputs, converts logical coordinates to physical pixels (DPI
awareness belongs at this layer), and delegates to the boundary. Tests inject a
fake and therefore never move the real cursor or send real input.

The default boundary uses Win32 ``SendInput`` for mouse and keyboard events and
``GetCursorPos``/``SetCursorPos``/``GetForegroundWindow``/
``SetForegroundWindow``/``GetWindowRect`` for state. All ctypes definitions are
declared at import time but no OS handle is opened until the default boundary
is constructed.
"""
from __future__ import annotations

import ctypes
from typing import Any, Protocol, runtime_checkable


# Logical-to-physical conversion baseline used by Windows (96 DPI = 100%).
_BASE_DPI = 96
_BUTTONS = frozenset({"left", "right"})
_DIRECTIONS = frozenset({"up", "down"})
_KEYCODES = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "alt": 0x12,
    "pause": 0x13,
    "capslock": 0x14,
    "escape": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "insert": 0x2D,
    "delete": 0x2E,
    "printscreen": 0x2C,
    "win": 0x5B,
    "menu": 0x5D,
    "numlock": 0x90,
    "scrolllock": 0x91,
}
for _digit in range(10):
    _KEYCODES[str(_digit)] = 0x30 + _digit
for _letter in "abcdefghijklmnopqrstuvwxyz":
    _KEYCODES[_letter] = 0x41 + (ord(_letter) - ord("a"))
for _n in range(1, 25):
    _KEYCODES[f"f{_n}"] = 0x6F + _n

_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_WHEEL = 0x0800
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_WHEEL_DELTA = 120
_LOGPIXELSX = 88
_SM_CXSCREEN = 0
_SM_CXVIRTUALSCREEN = 78


@runtime_checkable
class DesktopControl(Protocol):
    """The exact surface consumed by executor/evidence tasks."""

    def cursor_pos(self) -> tuple[int, int]: ...

    def move_cursor(self, x: int, y: int) -> None: ...

    def click(self, x: int, y: int, button: str, clicks: int) -> None: ...

    def scroll(self, x: int, y: int, direction: str, amount: int) -> None: ...

    def type_text(self, text: str) -> None: ...

    def press_keys(self, keys: list[str]) -> None: ...

    def focus_window(self, hwnd: int) -> None: ...

    def window_rect(self, hwnd: int) -> tuple[int, int, int, int]: ...


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


class Win32OSBoundary:
    """Default injected boundary: the only owner of raw Win32 calls."""

    def __init__(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self._user32 = user32
        self._gdi32 = gdi32

        user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
        user32.GetCursorPos.restype = ctypes.c_int
        user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        user32.SetCursorPos.restype = ctypes.c_int
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.SetForegroundWindow.restype = ctypes.c_int
        user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
        user32.GetWindowRect.restype = ctypes.c_int
        user32.SendInput.argtypes = [
            ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int
        ]
        user32.SendInput.restype = ctypes.c_uint
        user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
        user32.MapVirtualKeyW.restype = ctypes.c_uint

        gdi32.GetDeviceCaps.argtypes = [ctypes.c_void_p, ctypes.c_int]
        gdi32.GetDeviceCaps.restype = ctypes.c_int
        user32.GetDC.argtypes = [ctypes.c_void_p]
        user32.GetDC.restype = ctypes.c_void_p
        user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.ReleaseDC.restype = ctypes.c_int

    def get_cursor_pos(self) -> tuple[int, int]:
        point = _POINT()
        if not self._user32.GetCursorPos(ctypes.byref(point)):
            raise OSError(ctypes.get_last_error(), "GetCursorPos failed")
        return int(point.x), int(point.y)

    def set_cursor_pos(self, x: int, y: int) -> None:
        if not self._user32.SetCursorPos(x, y):
            raise OSError(ctypes.get_last_error(), "SetCursorPos failed")

    @classmethod
    def _send(cls, *inputs: _INPUT) -> None:
        array = (_INPUT * len(inputs))(*inputs)
        sent = cls._user32.SendInput(len(array), array, ctypes.sizeof(_INPUT))
        if sent != len(array):
            raise OSError(ctypes.get_last_error(), "SendInput partially failed")

    @classmethod
    def _mouse(cls, x: int, y: int, flags: int, data: int = 0) -> _INPUT:
        return _INPUT(
            type=_INPUT_MOUSE,
            mi=_MOUSEINPUT(x, y, data, flags, 0, None),
        )

    @classmethod
    def _key(cls, vk: int, scan: int, flags: int = 0) -> _INPUT:
        return _INPUT(
            type=_INPUT_KEYBOARD,
            ki=_KEYBDINPUT(vk, scan, flags, 0, None),
        )

    def click(self, x: int, y: int, button: str, clicks: int) -> None:
        down = _MOUSEEVENTF_LEFTDOWN if button == "left" else _MOUSEEVENTF_RIGHTDOWN
        up = _MOUSEEVENTF_LEFTUP if button == "left" else _MOUSEEVENTF_RIGHTUP
        events: list[_INPUT] = []
        for _ in range(clicks):
            events.extend((
                self._mouse(0, 0, down),
                self._mouse(0, 0, up),
            ))
        self._send(*events)

    def scroll(self, x: int, y: int, direction: str, amount: int) -> None:
        delta = _WHEEL_DELTA * amount * (1 if direction == "up" else -1)
        self._send(self._mouse(0, 0, _MOUSEEVENTF_WHEEL, delta & 0xFFFFFFFF))

    def type_text(self, text: str) -> None:
        for char in text:
            scan = self._user32.MapVirtualKeyW(ord(char), 0)
            self._send(
                self._key(0, scan, _KEYEVENTF_UNICODE),
                self._key(0, scan, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP),
            )

    def press_keys(self, keys: list[str]) -> None:
        events: list[_INPUT] = []
        for key in keys:
            vk = _KEYCODES[key]
            scan = self._user32.MapVirtualKeyW(vk, 0)
            events.append(self._key(vk, scan))
        for key in reversed(keys):
            vk = _KEYCODES[key]
            scan = self._user32.MapVirtualKeyW(vk, 0)
            events.append(self._key(vk, scan, _KEYEVENTF_KEYUP))
        self._send(*events)

    def get_foreground_window(self) -> int:
        return int(self._user32.GetForegroundWindow() or 0)

    def set_foreground_window(self, hwnd: int) -> None:
        if not self._user32.SetForegroundWindow(hwnd):
            raise OSError(ctypes.get_last_error(), "SetForegroundWindow failed")

    def get_window_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        rect = _RECT()
        if not self._user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise OSError(ctypes.get_last_error(), "GetWindowRect failed")
        return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)

    def get_dpi(self) -> int:
        """Query real system DPI from the device context; never guess."""
        dc = self._user32.GetDC(None)
        if not dc:
            raise OSError(ctypes.get_last_error(), "GetDC failed")
        try:
            dpi = int(self._gdi32.GetDeviceCaps(dc, _LOGPIXELSX))
        finally:
            self._user32.ReleaseDC(None, dc)
        if dpi <= 0:
            raise OSError("GetDeviceCaps returned an invalid DPI")
        return dpi


class Win32DesktopControl:
    """First-party adapter implementing :class:`DesktopControl`."""

    def __init__(self, os: Any | None = None) -> None:
        self._os = os if os is not None else Win32OSBoundary()

    def _scale(self) -> float:
        get_dpi = getattr(self._os, "get_dpi", None)
        if get_dpi is None:
            return 1.0
        dpi = int(get_dpi())
        if dpi <= 0:
            raise ValueError("DPI must be positive")
        return dpi / _BASE_DPI

    @staticmethod
    def _physical(value: int, scale: float) -> int:
        return int(round(value * scale))

    def cursor_pos(self) -> tuple[int, int]:
        scale = self._scale()
        x, y = self._os.get_cursor_pos()
        inverse = 1.0 / scale
        return self._physical(int(x), inverse), self._physical(int(y), inverse)

    def move_cursor(self, x: int, y: int) -> None:
        scale = self._scale()
        self._os.set_cursor_pos(
            self._physical(int(x), scale), self._physical(int(y), scale)
        )

    def click(self, x: int, y: int, button: str, clicks: int) -> None:
        if button not in _BUTTONS:
            raise ValueError(f"unsupported mouse button: {button}")
        if clicks < 1:
            raise ValueError("clicks must be positive")
        scale = self._scale()
        self._os.click(
            self._physical(int(x), scale),
            self._physical(int(y), scale),
            button,
            int(clicks),
        )

    def scroll(self, x: int, y: int, direction: str, amount: int) -> None:
        if direction not in _DIRECTIONS:
            raise ValueError(f"unsupported scroll direction: {direction}")
        if amount < 1:
            raise ValueError("amount must be positive")
        scale = self._scale()
        self._os.scroll(
            self._physical(int(x), scale),
            self._physical(int(y), scale),
            direction,
            int(amount),
        )

    def type_text(self, text: str) -> None:
        self._os.type_text(str(text))

    def press_keys(self, keys: list[str]) -> None:
        normalized = [str(key).casefold() for key in keys]
        for key in normalized:
            if key not in _KEYCODES:
                raise ValueError(f"unsupported key: {key}")
        self._os.press_keys(normalized)

    def focus_window(self, hwnd: int) -> None:
        self._os.set_foreground_window(int(hwnd))

    def window_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        scale = self._scale()
        inverse = 1.0 / scale
        rect = self._os.get_window_rect(int(hwnd))
        if len(rect) != 4:
            raise ValueError("window_rect must have exactly four values")
        left, top, right, bottom = (int(value) for value in rect)
        return (
            self._physical(left, inverse),
            self._physical(top, inverse),
            self._physical(right, inverse),
            self._physical(bottom, inverse),
        )


__all__ = ["DesktopControl", "Win32DesktopControl", "Win32OSBoundary"]
