"""Run the behavioral eval case set under pytest.

The standalone runner (python eval/run_eval.py) gives the human scorecard;
this keeps the same cases inside the normal `pytest` sweep so a routing,
tier, screen-steer or turn-contract regression fails the suite like any
other break — no separate discipline required."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval.cases import CASES  # noqa: E402
from eval.run_eval import run_case  # noqa: E402

OFFLINE = [c for c in CASES if c["check"][0] != "turn"]
TURNS = [c for c in CASES if c["check"][0] == "turn"]


@pytest.mark.parametrize("case", OFFLINE, ids=[c["id"] for c in OFFLINE])
def test_behavior_case(case):
    run_case(case)


@pytest.mark.parametrize("case", TURNS, ids=[c["id"] for c in TURNS])
def test_scripted_turn_case(case):
    run_case(case)
