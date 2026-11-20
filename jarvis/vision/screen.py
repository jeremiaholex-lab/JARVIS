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

# Enumerate visible top-level windows that have a title. This is what gives
# JARVIS the "different apps" the user actually asked about: Chrome is on
# screen, but so are the three background windows the whole-screen capture
# cannot separate from the desktop behind them.
_LIST_WINDOWS_PS1 = """
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class WL {
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern bool EnumWindows(EnumProc c, IntPtr l);
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextLength(IntPtr h);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint p);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
  public static string Dump() {
    var o = new StringBuilder();
    EnumWindows((h,l)=>{
      if (!IsWindowVisible(h)) return true;
      int len = GetWindowTextLength(h);
      if (len <= 0) return true;
      var t = new StringBuilder(512);
      int n = GetWindowTextW(h, t, t.Capacity);
      if (n <= 0) return true;
      var c = new StringBuilder(256); GetClassNameW(h, c, c.Capacity);
      uint pid; GetWindowThreadProcessId(h, out pid);
      string proc;
      try { proc = System.Diagnostics.Process.GetProcessById((int)pid).ProcessName; }
      catch { proc = "?"; }
      o.Append((long)h).Append('|').Append(pid).Append('|').Append(proc)
       .Append('|').Append(c.ToString().Replace("|","/")).Append('|')
       .Append(Convert.ToBase64String(Encoding.UTF8.GetBytes(
           t.ToString().Replace((char)13,(char)32).Replace((char)10,(char)32))))
       .Append((char)10);
      return true;
    }, IntPtr.Zero);
    return o.ToString();
  }
}
"@
[WL]::Dump()
"""

# PrintWindow with PW_RENDERFULLCONTENT (2) captures the window's own pixels,
# including GPU-composited surfaces, whether or not it is on top. Targeting
# is by hwnd, resolved in Python from the list above: window titles go out
# base64-encoded because Windows PowerShell pipes text in an OEM codepage,
# and a mangled title searched against the live title finds nothing.
# Minimized windows are reported instead of captured: PrintWindow returns a
# blank frame for them, and a blank frame described by a vision model is
# worse than no frame at all.
_CAPTURE_WINDOW_PS1 = """
param([string]$Out, [long]$Hwnd)
Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class WC {
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L,T,R,B; }
  public static string Shot(IntPtr h, string outPath) {
    if (h == IntPtr.Zero || !IsWindowVisible(h)) return "NOWINDOW";
    RECT r; if (!GetWindowRect(h, out r)) return "NORECT";
    int w = r.R - r.L, hh = r.B - r.T;
    if (w < 20 || hh < 20) return "MINIMIZED";
    var bmp = new System.Drawing.Bitmap(w, hh);
    using (var g = System.Drawing.Graphics.FromImage(bmp)) {
      IntPtr hdc = g.GetHdc();
      bool ok = PrintWindow(h, hdc, 2);
      g.ReleaseHdc(hdc);
      if (!ok) { bmp.Dispose(); return "FAIL"; }
    }
    bmp.Save(outPath, System.Drawing.Imaging.ImageFormat.Png);
    bmp.Dispose();
    return w + "x" + hh;
  }
}
"@
[WC]::Shot([IntPtr]$Hwnd, $Out)
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


@dataclass(slots=True)
class WindowInfo:
    """One capturable top-level window. `hwnd` is opaque; it is only ever
    handed back to the capture script, never interpreted here."""

    hwnd: str
    pid: int
    process: str
    class_name: str
    title: str

    @property
    def label(self) -> str:
        return f"{self.title} ({self.process})"


class ScreenCaptureError(RuntimeError):
    pass


def _png_dimensions(png: bytes) -> tuple[int, int]:
    if len(png) < 24 or not png.startswith(_PNG_MAGIC):
        raise ScreenCaptureError("capture did not produce a PNG")
    width, height = struct.unpack(">II", png[16:24])
    return width, height


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    # utf-8 explicitly: PowerShell on Windows pipes in the OEM codepage by
    # default, which lossy-decodes titles containing non-ASCII characters.
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


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
        self._window_cached: dict[str, tuple[float, Screen]] = {}
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

    # ------------------------------------------------------------ per-window

    def list_windows(self) -> list[WindowInfo]:
        """Visible, titled top-level windows. Empty on non-Windows hosts."""
        if os.name != "nt":
            return []
        out = self._run_ps(_LIST_WINDOWS_PS1)
        windows: list[WindowInfo] = []
        for line in out.splitlines():
            parts = line.strip().split("|")
            if len(parts) != 5:
                continue
            try:
                hwnd, pid, proc, cls, raw_title = (parts[0], int(parts[1]), parts[2],
                                                   parts[3], parts[4])
                title = base64.b64decode(raw_title).decode("utf-8", "replace")
            except (ValueError, Exception):
                continue
            if not title:
                continue
            windows.append(WindowInfo(hwnd=hwnd, pid=pid, process=proc,
                                      class_name=cls, title=title))
        return windows

    def capture_window(self, query: str) -> Screen:
        """Capture one window's own pixels by title or process name.

        Raises ScreenCaptureError with an explanatory message when nothing
        matches, several windows match, or the match cannot be captured
        (minimized) — the tool layer turns those into a candidate list the
        model can ask the user about.
        """
        if os.name != "nt":
            raise ScreenCaptureError("per-window capture is only supported on Windows")
        query = (query or "").strip()
        if not query:
            raise ScreenCaptureError("no window given")
        key = query.casefold()
        with self._lock:
            hit = self._window_cached.get(key)
            if hit and time.time() - hit[0] < self._ttl:
                return hit[1]

        windows = self.list_windows()
        titled = [w for w in windows if key in w.title.casefold()]
        procs = [w for w in windows if key in w.process.casefold()]
        matches = titled or procs
        if not matches:
            raise ScreenCaptureError(
                f"no open window matches {query!r}; open windows: "
                + ", ".join(sorted({w.title for w in windows})[:12]))
        if len(matches) > 1:
            # Picking one arbitrarily would answer a question about a window
            # the user did not name. Hand the list back; the model asks.
            raise ScreenCaptureError(
                f"{query!r} matches {len(matches)} windows: "
                + ", ".join(sorted({w.title for w in matches})[:8])
                + " — retry with one of those exact titles")
        win = matches[0]

        out_dir = Path(tempfile.mkdtemp(prefix="jarvis-window-"))
        out_path = out_dir / "window.png"
        try:
            ps = (_CAPTURE_WINDOW_PS1
                  .replace("param([string]$Out, [long]$Hwnd)",
                           f'$Out = "{_ps_str(out_path.as_posix())}"; '
                           f'$Hwnd = {int(win.hwnd)};'))
            out = self._run_ps(ps)
            status = (out or "").strip().splitlines()
            status = status[-1] if status else ""
            if status == "NOWINDOW":
                raise ScreenCaptureError(f"window closed before capture: {win.label}")
            if status == "MINIMIZED":
                raise ScreenCaptureError(
                    f"{win.label} is minimized — ask the user to restore it, "
                    "or read the whole screen instead")
            if "x" not in status:
                raise ScreenCaptureError(f"window capture failed: {status or 'no output'}")
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
            raise ScreenCaptureError(f"implausible window capture ({len(png)} bytes)")
        width, height = _png_dimensions(png)
        screen = Screen(png=png, width=width, height=height,
                        captured_at=time.time(), backend="windows-printwindow")
        with self._lock:
            self._window_cached[key] = (time.time(), screen)
        return screen

    @staticmethod
    def _run_ps(script: str) -> str:
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            raise ScreenCaptureError("no PowerShell available for window capture")
        tmp = Path(tempfile.mkdtemp(prefix="jarvis-ps-"))
        script_path = tmp / "probe.ps1"
        try:
            # utf-8-sig: Windows PowerShell 5.1 reads BOM-less files as ANSI,
            # which mangles any non-ASCII window title embedded in the script
            # (the title then matches nothing — a silent NOWINDOW).
            script_path.write_text(script, encoding="utf-8-sig")
            proc = _run([powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                         "-File", str(script_path)], timeout=30)
        finally:
            for stale in tmp.glob("*"):
                try:
                    stale.unlink()
                except OSError:
                    pass
            try:
                tmp.rmdir()
            except OSError:
                pass
        if proc.returncode != 0:
            raise ScreenCaptureError(
                f"window probe failed (rc={proc.returncode}): {(proc.stderr or '').strip()[:200]}")
        return proc.stdout or ""


_PSNES = ("\\", '"', "'", "$", "`", "[", "]", "{", "}")


def _ps_str(value: str) -> str:
    """Escape a string for the double-quoted PowerShell assignments above.

    Inside a PS double-quoted string only `"`, `$` and the backtick are
    live; window titles are arbitrary text and must not become script syntax.
    """
    return value.replace("`", "``").replace('"', '`"').replace("$", "`$")


__all__ = ["Screen", "ScreenCapture", "ScreenCaptureError", "WindowInfo"]
