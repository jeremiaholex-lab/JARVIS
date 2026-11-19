"""Screen perception (design §6.7): capture what is on the display.

No Pillow, no mss, no extra dependency — this box has neither, and the design
keeps the footprint small on a 16 GB laptop. Windows uses GDI through
PowerShell's System.Drawing; macOS uses the built-in `screencapture`; Linux
uses `import` (ImageMagick) or `mss` if someone installs it later.

Privacy note, because it matters more here than anywhere else in JARVIS:
capturing the screen is free (it never leaves the machine), but *describing*
it needs a vision model, and the only vision model reachable on this machine
is the cloud one. See jarvis.vision.describe — it refuses to send pixels
without an explicit opt-in.
"""
from __future__ import annotations

import base64
import logging
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_logger = logging.getLogger(__name__)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_MAX_SCREEN_BYTES = 2_500_000  # a screen this big is a misconfigured capture
_CACHE_TTL_S = 2.0

_CAPTURE_PS1 = """
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$b = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap($b.Width, $b.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
$bmp.Save($args[0], [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
"""


@dataclass(slots=True)
class Screen:
    png: bytes
    width: int
    height: int
    captured_at: float
    backend: str

    @property
    def data_url(self) -> str:
        return "data:image/png;base64," + base64.b64encode(self.png).decode("ascii")


class ScreenCaptureError(RuntimeError):
    pass


def _png_dimensions(png: bytes) -> tuple[int, int]:
    if len(png) < 24 or not png.startswith(_PNG_MAGIC):
        raise ScreenCaptureError("capture did not produce a PNG")
    width, height = struct.unpack(">II", png[16:24])
    return width, height


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _capture_windows(out_path: Path) -> str:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        raise ScreenCaptureError("no PowerShell available for screen capture")
    script = out_path.with_suffix(".ps1")
    script.write_text(_CAPTURE_PS1, encoding="utf-8")
    proc = _run([powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(script), str(out_path).replace("\\", "/")], timeout=30)
    if proc.returncode != 0 or not out_path.exists():
        raise ScreenCaptureError(
            f"GDI capture failed (rc={proc.returncode}): {(proc.stderr or '').strip()[:200]}"
        )
    return "windows-gdi"


def _capture_posix(out_path: Path) -> str:
    if sys.platform == "darwin":
        if not shutil.which("screencapture"):
            raise ScreenCaptureError("screencapture not found")
        proc = _run(["screencapture", "-x", "-t", "png", str(out_path)], timeout=30)
        backend = "screencapture"
    elif shutil.which("import"):
        proc = _run(["import", "-window", "root", "-silent", str(out_path)], timeout=30)
        backend = "imagemagick"
    elif shutil.which("mss"):
        raise ScreenCaptureError("install mss to use it; falling back is not possible here")
    else:
        raise ScreenCaptureError("no screen capture tool available on this platform")
    if proc.returncode != 0 or not out_path.exists():
        raise ScreenCaptureError(f"{backend} capture failed (rc={proc.returncode})")
    return backend


class ScreenCapture:
    """Short-cached screen grab.

    The cache exists so a model that calls the tool twice in one turn (small
    models do this) does not pay another ~1s GDI round trip.
    """

    def __init__(self, ttl: float = _CACHE_TTL_S) -> None:
        self._ttl = ttl
        self._cached: Screen | None = None
        self._lock = threading.Lock()

    @staticmethod
    def available() -> bool:
        """Is there any capture backend on this host? Checked without grabbing."""
        if os.name == "nt":
            return bool(shutil.which("powershell") or shutil.which("pwsh"))
        if sys.platform == "darwin":
            return bool(shutil.which("screencapture"))
        return bool(shutil.which("import") or shutil.which("mss"))

    def grab(self) -> Screen:
        with self._lock:
            now = time.time()
            if self._cached and now - self._cached.captured_at < self._ttl:
                return self._cached
            screen = self._grab()
            self._cached = screen
            return screen

    def _grab(self) -> Screen:
        out_dir = Path(tempfile.mkdtemp(prefix="jarvis-screen-"))
        out_path = out_dir / "screen.png"
        try:
            backend = _capture_windows(out_path) if os.name == "nt" else _capture_posix(out_path)
            png = out_path.read_bytes()
        finally:
            for stale in out_dir.glob("*"):
                try:
                    stale.unlink()
                except OSError:
                    pass
            try:
                out_dir.rmdir()
            except OSError:
                pass

        if not png or len(png) > _MAX_SCREEN_BYTES:
            raise ScreenCaptureError(f"implausible capture size ({len(png)} bytes)")
        width, height = _png_dimensions(png)
        _logger.debug("screen captured %dx%d via %s", width, height, backend)
        return Screen(png=png, width=width, height=height, captured_at=time.time(), backend=backend)


__all__ = ["Screen", "ScreenCapture", "ScreenCaptureError"]
