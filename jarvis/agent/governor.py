"""Memory pressure governor for the orb host (design principle: instrument,
not guess).

This machine has 15.8 GB total and the voice stack (whisper small int8 +
Kokoro + Silero + openWakeWord + an Ollama KV cache) has already been seen
sitting at ~2.7 GB resident with ~3 GB free. A background kill by the OS is
the worst failure mode available: the orb window survives and looks alive
while being completely deaf. The shell now relaunches that crash, but the
right move is to not earn it — shed optional work before the box does.

Three states, decided by available RAM with hysteresis so a value oscillating
around a threshold cannot flap the system between modes every sample:

    ok        full behaviour
    warn      cheap reductions: whisper beam_size 1, no post-turn user-model
              learning, no proactive speech (text alerts still queue)
    critical  additionally stop starting NEW background work: task runner,
              watchers, proactive cycles. Turns in flight are never cut —
              the user's own request always finishes.

The sampler is injectable; the module never raises on measurement failure
— an unreadable memory count reads as "ok", which is the pre-feature
behaviour, never a reason to disable the assistant.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

_logger = logging.getLogger(__name__)

_GB = 1024 ** 3

OK, WARN, CRITICAL = "ok", "warn", "critical"
_SEVERITY = {OK: 0, WARN: 1, CRITICAL: 2}


def default_sampler() -> Optional[float]:
    """Available physical memory in GB, or None when unmeasurable."""
    try:
        import psutil
        return psutil.virtual_memory().available / _GB
    except Exception:
        pass
    try:
        import ctypes
        class _MSG(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_uint32),
                        ("dwMemoryLoad", ctypes.c_uint32),
                        ("ullTotalPhys", ctypes.c_uint64),
                        ("ullAvailPhys", ctypes.c_uint64),
                        ("ullTotalPageFile", ctypes.c_uint64),
                        ("ullAvailPageFile", ctypes.c_uint64),
                        ("ullTotalVirtual", ctypes.c_uint64),
                        ("ullAvailVirtual", ctypes.c_uint64),
                        ("ullAvailExtendedVirtual", ctypes.c_uint64)]
        stat = _MSG()
        stat.dwLength = ctypes.sizeof(_MSG)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return stat.ullAvailPhys / _GB
    except Exception:
        pass
    return None


@dataclass(slots=True)
class Pressure:
    state: str = OK
    available_gb: Optional[float] = None      # None = unmeasurable
    since: float = field(default_factory=time.time)

    @property
    def measurable(self) -> bool:
        return self.available_gb is not None

    def as_dict(self) -> dict:
        return {"state": self.state,
                "available_gb": (round(self.available_gb, 2)
                                  if self.available_gb is not None else None)}


class MemoryGovernor:
    """Stateful three-level governor with hysteresis on every transition.

    `warn_gb`/`critical_gb` mark entry; a state is only left when memory
    recovers by `hysteresis_gb` above the same line, so a 0.05 GB wobble
    cannot repeatedly shed and restore background work. Upgrades (ok→warn,
    warn→critical) take effect immediately on the next sample; downgrades
    require the margin.
    """

    def __init__(self, *, warn_gb: float = 1.6, critical_gb: float = 0.9,
                 hysteresis_gb: float = 0.4, sample_interval_s: float = 5.0,
                 sampler: Callable[[], Optional[float]] | None = None) -> None:
        if critical_gb >= warn_gb:
            raise ValueError("critical_gb must be below warn_gb")
        self.warn_gb = warn_gb
        self.critical_gb = critical_gb
        self.hysteresis_gb = max(0.0, hysteresis_gb)
        self.sample_interval_s = sample_interval_s
        self._sampler = sampler or default_sampler
        self._lock = threading.Lock()
        self._last_sample = 0.0
        self._pressure = Pressure()

    # ------------------------------------------------------------------ API

    def pressure(self) -> Pressure:
        """Current state, re-sampled at most once per sample_interval_s."""
        now = time.monotonic()
        with self._lock:
            if now - self._last_sample >= self.sample_interval_s:
                self._last_sample = now
                self._advance(self._sampler())
            return self._pressure

    def _advance(self, avail: Optional[float]) -> None:
        prev = self._pressure.state
        if avail is None:
            # Unreadable: keep the last state (a misbehaving sensor must not
            # silently switch the assistant into eco mode or out of it).
            return
        raw = (CRITICAL if avail <= self.critical_gb
               else WARN if avail <= self.warn_gb else OK)
        if _SEVERITY[raw] >= _SEVERITY[prev]:
            # same level or worse: apply immediately, the box is degrading
            state = raw
        elif prev == CRITICAL and avail > self.critical_gb + self.hysteresis_gb:
            # recovery, with the margin: relax one level per sample, so a
            # momentary spike cannot restore every background job at once
            state = WARN
        elif prev == WARN and avail > self.warn_gb + self.hysteresis_gb:
            state = OK
        else:
            state = prev
        if state != prev:
            _logger.info("memory governor: %s -> %s (%.2f GB free)", prev, state, avail)
        self._pressure = Pressure(state=state, available_gb=avail,
                                  since=self._pressure.since if state == prev else time.time())

    # ----------------------------------------------------------- consumers

    def is_warn(self) -> bool:
        return self.pressure().state != OK

    def is_critical(self) -> bool:
        return self.pressure().state == CRITICAL

    def whisper_beam_size(self, configured: int) -> int:
        """Beam 1 under any pressure: beam_size 5 is the single biggest
        per-call CPU/RAM spike in STT and the quality delta is small."""
        return 1 if self.is_warn() else max(1, int(configured))

    def allow_background_work(self) -> bool:
        """Critical stops NEW background cycles. In-flight work and the
        user-facing turn always proceed."""
        return not self.is_critical()

    def allow_proactive_speech(self) -> bool:
        return not self.is_warn()

    def allow_post_turn_learning(self) -> bool:
        return not self.is_warn()

    def status(self) -> dict:
        return self.pressure().as_dict()


__all__ = ["MemoryGovernor", "Pressure", "OK", "WARN", "CRITICAL", "default_sampler"]
