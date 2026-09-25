"""Shared pytest fixtures."""

from __future__ import annotations

import numpy as np
import pytest

from streaming_stft import STFTConfig


@pytest.fixture
def rng() -> np.random.Generator:
    return np.random.default_rng(20260925)


@pytest.fixture
def random_signal(rng: np.random.Generator) -> np.ndarray:
    return rng.standard_normal(4096)


@pytest.fixture
def default_config() -> STFTConfig:
    return STFTConfig(nfft=256, hop=128, window="hann", center=True)
