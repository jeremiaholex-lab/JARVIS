"""Locally generated screenshot evidence for confirmation cards.

The capture itself comes from the existing ``jarvis.vision.screen`` facility.
Cropping, PNG encoding, and the sensitive-window check all happen on this
machine. This module has no network path at all: pixels are returned to the
caller as bytes and are never uploaded, including when vision consent exists.
"""
from __future__ import annotations

import struct
import zlib
from typing import Any

from jarvis.desktop.actions import redact_sensitive
from jarvis.desktop.win32_control import DesktopControl
from jarvis.vision.screen import Screen, ScreenCapture, ScreenCaptureError

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# Hard ceiling for a card crop; a larger image is a misconfigured capture.
_MAX_CROP_BYTES = 4_000_000


class SensitiveTargetError(RuntimeError):
    """Raised when the area belongs to a window that must not be shown."""


def _chunk(tag: bytes, data: bytes) -> bytes:
    body = tag + data
    return (struct.pack(">I", len(data)) + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))


def _encode_png(width: int, height: int, rows: list[bytes]) -> bytes:
    raw = b"".join(b"\x00" + row for row in rows)
    return (_PNG_MAGIC
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw))
            + _chunk(b"IEND", b""))


def _decode_rgb(png: bytes) -> tuple[int, int, list[bytes]]:
    """Decode the non-interlaced 8-bit PNGs produced by the screen grab.

    The GDI backend emits 8-bit RGBA (color type 6); the decoder also accepts
    grayscale (0), RGB (2), palette (3, 8-bit PLTE), and gray+alpha (4), and
    normalizes every variant to 3-channel RGB rows. Interlaced captures are
    rejected: no screen backend produces them.
    """
    if len(png) < 24 or not png.startswith(_PNG_MAGIC):
        raise ScreenCaptureError("capture did not produce a PNG")
    pos = 8
    width = height = 0
    color_type = bit_depth = interlace = None
    palette = b""
    compressed: list[bytes] = []
    while pos + 12 <= len(png):
        size = struct.unpack(">I", png[pos:pos + 4])[0]
        tag = png[pos + 4:pos + 8]
        data = png[pos + 8:pos + 8 + size]
        pos += 12 + size
        if tag == b"IHDR":
            if len(data) < 13:
                raise ScreenCaptureError("invalid PNG header")
            width, height, bit_depth, color_type, _comp, _filter, interlace = \
                struct.unpack(">IIBBBBB", data[:13])
        elif tag == b"PLTE":
            palette = data
        elif tag == b"IDAT":
            compressed.append(data)
        elif tag == b"IEND":
            break
    if not width or not height or not compressed:
        raise ScreenCaptureError("incomplete PNG capture")
    if bit_depth != 8 or interlace != 0:
        raise ScreenCaptureError("unsupported PNG format for card crop")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None or (color_type == 3 and len(palette) < 3):
        raise ScreenCaptureError("unsupported PNG format for card crop")
    raw = zlib.decompress(b"".join(compressed))
    stride = width * channels
    pixels: list[bytes] = []
    previous = bytes(stride)
    offset = 0
    for _ in range(height):
        if offset + 1 + stride > len(raw):
            raise ScreenCaptureError("truncated PNG capture")
        filter_type = raw[offset]
        row = bytearray(raw[offset + 1:offset + 1 + stride])
        offset += 1 + stride
        for index in range(stride):
            left = row[index - channels] if index >= channels else 0
            up = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 1:
                row[index] = (row[index] + left) & 0xFF
            elif filter_type == 2:
                row[index] = (row[index] + up) & 0xFF
            elif filter_type == 3:
                row[index] = (row[index] + ((left + up) >> 1)) & 0xFF
            elif filter_type == 4:
                p = left + up - upper_left
                distances = (abs(p - left), abs(p - up), abs(p - upper_left))
                predictor = (left, up, upper_left)[distances.index(min(distances))]
                row[index] = (row[index] + predictor) & 0xFF
            elif filter_type != 0:
                raise ScreenCaptureError("unsupported PNG row filter")
        pixels.append(bytes(row))
        previous = pixels[-1]

    # Normalize every variant to 3-channel RGB; the crop is re-encoded as RGB.
    rows: list[bytes] = []
    for row in pixels:
        converted = bytearray()
        for start in range(0, len(row), channels):
            pixel = row[start:start + channels]
            if color_type == 0:
                gray = pixel[0]
                converted += bytes((gray, gray, gray))
            elif color_type == 2:
                converted += pixel
            elif color_type == 3:
                entry = pixel[0]
                converted += palette[entry * 3:entry * 3 + 3]
            else:  # gray+alpha or RGBA: keep the first three channels.
                converted += pixel[:3]
        rows.append(bytes(converted))
    return width, height, rows


def _assert_sensitive_windows_hidden(capture: Any) -> None:
    """Reuse the shared redaction policy; never duplicate its patterns."""
    list_windows = getattr(capture, "list_windows", None)
    if list_windows is None:
        # No window lookup facility: fail closed rather than risk a sensitive
        # area ending up on a card.
        raise SensitiveTargetError("Sensitive window status is unavailable.")
    for window in list_windows():
        label = " ".join(str(getattr(window, field, "")) for field in
                         ("title", "label", "process", "class_name"))
        if redact_sensitive(label) != label:
            raise SensitiveTargetError(
                "Sensitive window is open; no screenshot crop was captured."
            )


def capture_target_crop(
    control: DesktopControl,
    x: int,
    y: int,
    radius: int = 160,
) -> bytes:
    """Capture a square PNG crop around ``(x, y)``, clipped to screen bounds.

    Sensitive windows veto the capture before any pixel is read. The control is
    used only for window geometry; it never moves the cursor or clicks.
    """
    x, y, radius = int(x), int(y), int(radius)
    if x < 0 or y < 0 or radius <= 0:
        raise ValueError("coordinates must be non-negative and radius positive")
    capture = ScreenCapture()
    _assert_sensitive_windows_hidden(capture)
    screen: Screen = capture.grab()
    width, height = int(screen.width), int(screen.height)
    if width <= 0 or height <= 0 or x >= width or y >= height:
        raise ScreenCaptureError("target is outside the capture area")

    # Clipping means dropping pixels outside the screen, never widening the
    # box: a card crop must not grow to 2*radius+2*(radius-offset) pixels.
    left = max(0, x - radius)
    top = max(0, y - radius)
    right = min(width, x + radius)
    bottom = min(height, y + radius)
    crop_width, crop_height = right - left, bottom - top

    source_width, source_height, rows = _decode_rgb(screen.png)
    if (source_width, source_height) != (width, height):
        raise ScreenCaptureError("capture dimensions do not match the PNG")
    cropped = [row[left * 3:right * 3] for row in rows[top:bottom]]
    png = _encode_png(crop_width, crop_height, cropped)
    if len(png) > _MAX_CROP_BYTES:
        raise ScreenCaptureError("crop exceeds the allowed size")
    return png


__all__ = ["capture_target_crop", "SensitiveTargetError"]
