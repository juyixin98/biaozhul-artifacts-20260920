"""Shared pytest configuration: make src/ importable and use an isolated log dir."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture()
def tmp_logger(tmp_path):
    from tenmem.runlog import RunLogger

    return RunLogger(tmp_path / "logs")


@pytest.fixture()
def engine(tmp_logger):
    from tenmem.service import Engine

    return Engine(tmp_logger)
