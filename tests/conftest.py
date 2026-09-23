"""Shared pytest helpers."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from cbmc.checker import CheckConfig, check_contract
from cbmc.fixtures import load_example
from cbmc.replay import replay_trace


@pytest.fixture
def run():
    """Return a helper that checks a fixture and replays any counterexample."""
    def _run(fixture_id, steps=10, timeout_ms=10_000, checks=None):
        contract = load_example(fixture_id)
        cfg = CheckConfig(steps=steps, timeout_ms=timeout_ms,
                          checks=tuple(checks or (
                              "nonnegative", "conservation", "targets")))
        result = check_contract(contract, cfg)
        report = None
        if result.trace:
            report = replay_trace(contract, result.trace)
        return contract, result, report
    return _run
