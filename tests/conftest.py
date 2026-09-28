"""Shared pytest fixtures and sys.path fallback.

Every test that performs a computation runs under a unique run id and gets an
isolated temp data directory, so test logs/database rows can be correlated
with their inputs without polluting the project ``data/`` tree.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sample_size_planner.config import Settings  # noqa: E402
from sample_size_planner.api.service import PlanningService  # noqa: E402
from sample_size_planner.evidence.run_log import RunIdentity, RunLogger  # noqa: E402


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "data" / "test.db",
        runs_dir=tmp_path / "logs" / "runs",
        log_dir=tmp_path / "logs",
        fixture_dir=ROOT / "data",
    )


@pytest.fixture
def service(tmp_settings: Settings) -> PlanningService:
    return PlanningService(tmp_settings)


@pytest.fixture
def run_logger(tmp_settings: Settings):
    def _make(purpose: str = "test"):
        run = RunIdentity.new(purpose)
        logger = RunLogger(run, tmp_settings.runs_dir / f"{run.run_id}.jsonl", echo=False)
        return run, logger
    return _make


@pytest.fixture
def scenarios() -> dict:
    with (ROOT / "data" / "fixtures" / "scenarios.json").open() as fh:
        return json.load(fh)


@pytest.fixture
def rng() -> np.random.Generator:
    return np.random.default_rng(20260927)
