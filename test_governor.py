"""Memory governor: thresholds, hysteresis, and what each state switches off.

The sampler is injected; no test here touches psutil or the real machine."""
import pytest

from jarvis.agent.governor import (CRITICAL, OK, WARN, MemoryGovernor,
                                   Pressure)


class ScriptedSampler:
    def __init__(self, *values):
        self.values = list(values)
        self.n = 0

    def __call__(self):
        v = self.values[min(self.n, len(self.values) - 1)]
        self.n += 1
        return v


def gov(*values, **kw):
    g = MemoryGovernor(sampler=ScriptedSampler(*values), sample_interval_s=0.0, **kw)
    return g


def test_plain_thresholds():
    g = gov(4.0, 1.2, 0.5)
    assert g.pressure().state == OK
    assert g.pressure().state == WARN
    assert g.pressure().state == CRITICAL


def test_worsening_applies_immediately():
    g = gov(4.0, 0.5)          # ok -> critical in one sample, no margin needed
    g.pressure()
    assert g.pressure().state == CRITICAL


def test_recovery_requires_the_margin_and_relaxes_one_level_at_a_time():
    # ok(4.0) -> critical(0.5) -> 1.0 is above the critical line but not by
    # the 0.4 margin -> still critical -> 2.1 clears critical+margin, so one
    # level up (warn), even though raw says ok -> 2.1 clears warn+margin too.
    g = gov(4.0, 0.5, 1.0, 2.1, 2.1)
    seen = [g.pressure().state for _ in range(5)]
    assert seen == [OK, CRITICAL, CRITICAL, WARN, OK]


def test_oscillation_near_the_warn_line_does_not_flap():
    # 1.55/1.65 straddles warn_gb=1.6: once warned, leaving needs 1.6+0.4.
    g = gov(1.55, 1.65, 1.55, 1.65)
    states = [g.pressure().state for _ in range(4)]
    assert states[0] == WARN           # entry is immediate (below warn line)
    assert all(s == WARN for s in states)


def test_unmeasurable_sensor_keeps_the_last_state():
    g = gov(4.0, 0.5, None, None)
    assert g.pressure().state == OK
    assert g.pressure().state == CRITICAL
    assert g.pressure().state == CRITICAL     # None does not downgrade


def test_consumers_switch_off_the_right_things():
    g = gov(1.2)                             # warn
    assert g.whisper_beam_size(5) == 1
    assert g.allow_background_work() is True # warn still runs background work
    assert g.allow_proactive_speech() is False
    assert g.allow_post_turn_learning() is False
    g = gov(0.5)                             # critical
    g.pressure()
    assert g.allow_background_work() is False
    assert g.whisper_beam_size(5) == 1


def test_ok_state_is_the_untouched_default():
    g = gov(6.0)
    assert g.whisper_beam_size(5) == 5
    assert g.allow_background_work() is True
    assert g.allow_proactive_speech() is True
    assert g.allow_post_turn_learning() is True


def test_sample_interval_bounds_the_syscall():
    calls = {"n": 0}
    def sampler():
        calls["n"] += 1
        return 2.0
    g = MemoryGovernor(sampler=sampler, sample_interval_s=60.0)
    g.pressure(); g.pressure(); g.pressure()
    assert calls["n"] == 1


def test_misconfigured_thresholds_are_rejected():
    with pytest.raises(ValueError):
        MemoryGovernor(warn_gb=0.5, critical_gb=0.9)


def test_status_shape_for_the_api():
    g = gov(1.2)
    st = g.status()
    assert st["state"] == WARN
    assert st["available_gb"] == 1.2
