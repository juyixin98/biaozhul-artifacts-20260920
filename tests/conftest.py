"""Shared pytest fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def tiny_fixture() -> dict:
    """Reference answers derived by hand arithmetic, not by the implementation."""
    return json.loads((FIXTURE_DIR / "tiny_weights.json").read_text())


@pytest.fixture
def tiny_arrays(tiny_fixture):
    units = tiny_fixture["units"]
    a = np.array([u["a"] for u in units], dtype=np.int8)
    p = np.array([u["p"] for u in units], dtype=np.float64)
    y = np.array([u["y"] for u in units], dtype=np.float64)
    # The kernel consumes only (A, Y, p) here; X is irrelevant to weight math.
    return a, y, p
