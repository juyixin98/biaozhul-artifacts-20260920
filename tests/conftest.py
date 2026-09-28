"""Local synthetic fixtures shared by tests.

All data is generated here (deterministic seeds); no external resources or
real business data are used.
"""
from __future__ import annotations

import numpy as np
import pytest

from tensor_backend.tensor import Tensor


@pytest.fixture
def rng() -> np.random.Generator:
    return np.random.default_rng(20260928)


@pytest.fixture
def matrix_3x4() -> tuple[Tensor, np.ndarray]:
    arr = np.arange(12.0).reshape(3, 4)
    return Tensor.from_values(arr), arr


@pytest.fixture
def cube_2x3x4() -> tuple[Tensor, np.ndarray]:
    arr = np.arange(24.0).reshape(2, 3, 4)
    return Tensor.from_values(arr), arr


@pytest.fixture
def linear_problem(rng):
    # y = X @ w_true + b_true + small noise
    n, d = 64, 3
    x = rng.normal(size=(n, d))
    w_true = np.array([[1.5], [-2.0], [0.5]])
    b_true = 0.25
    y = x @ w_true + b_true + rng.normal(scale=1e-3, size=(n, 1))
    return Tensor.from_values(x), Tensor.from_values(y), w_true, b_true
