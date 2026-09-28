"""Pytest fixtures and path bootstrap."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import BudgetConfig, NumericConfig  # noqa: E402


@pytest.fixture
def budget() -> BudgetConfig:
    return BudgetConfig()


@pytest.fixture
def tight_budget() -> BudgetConfig:
    return BudgetConfig(
        max_degree=8,
        max_coefficient_bits=256,
        max_sturm_pairs=5_000,
        max_bisection_depth=20,
        max_roots=16,
    )


@pytest.fixture
def numeric() -> NumericConfig:
    return NumericConfig(mpmath_prec=113, verify_tolerance=1e-24)
