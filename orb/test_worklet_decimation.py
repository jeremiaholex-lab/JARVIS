"""Runs the microphone-downsampler regression checks as part of pytest.

The checks themselves live in test_worklet_decimation.js because the artifact
under test is an AudioWorklet: measuring JS sample math in Python would mean
testing a translation rather than the shipped code.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).with_name("test_worklet_decimation.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_mic_decimation_checks_pass():
    result = subprocess.run(
        ["node", str(JS)], capture_output=True, text=True, timeout=180, cwd=str(JS.parent)
    )
    output = (result.stdout or "") + (result.stderr or "")
    # Guard against a silently vacuous run: the suite must actually execute the
    # device-rate checks, not skip them because extraction changed shape.
    assert "48.0 kHz: duration" in output, output[-500:]
    assert "no audio dropped" in output, output[-500:]
    assert result.returncode == 0, output
