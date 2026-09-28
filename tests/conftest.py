"""Shared pytest configuration."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sym_eig.config import Settings  # noqa: E402


@pytest.fixture
def settings() -> Settings:
    # Generous defaults for correct-result tests; individual tests override
    # the budget or tolerances to exercise failure paths.
    return Settings(max_n=256, max_iters=30, reference_max_n=24)


@pytest.fixture
def strict_settings() -> Settings:
    return Settings(
        max_n=256,
        max_iters=30,
        reference_max_n=24,
        residual_rtol=1e-8,
        orthogonality_tol=1e-8,
        reconstruction_rtol=1e-8,
    )


@pytest.fixture
def rng() -> np.random.Generator:
    return np.random.default_rng(20260927)
