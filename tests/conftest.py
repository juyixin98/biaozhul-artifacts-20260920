"""Shared pytest fixtures.

Every test runs against an isolated tmp DB and tmp log directory; no test
touches the developer's data/plans.db.  RunLoggers propagate to the root
logger (no console handler attached), and Python's lastResort handler only
emits WARNING+, so INFO steps stay in the per-run files without flooding
test output.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ssp.config import Settings, reset_settings  # noqa: E402
from ssp.diagnostics import RunLogger  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SSP_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SSP_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SSP_LOG_LEVEL", "INFO")
    monkeypatch.setenv("SSP_MC_DEFAULT_TRIALS", "2500")
    monkeypatch.setenv("SSP_EXACT_ONE_SAMPLE_CAP", "200000")
    monkeypatch.setenv("SSP_EXACT_TWO_SAMPLE_TOTAL_CAP", "4000")
    reset_settings()
    yield
    reset_settings()


@pytest.fixture()
def settings(isolated_env) -> Settings:
    from ssp.config import get_settings

    return get_settings()


@pytest.fixture()
def logger(settings) -> RunLogger:
    return RunLogger("test-run", "fp-test")
