"""Continuity / correctness analysis helpers used by tests and reports."""

from __future__ import annotations

import numpy as np


def max_abs_error(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.shape != b.shape:
        raise ValueError(f"shape mismatch: {a.shape} vs {b.shape}")
    if a.size == 0:
        return 0.0
    return float(np.max(np.abs(a - b)))


def boundary_jumps(y: np.ndarray, block_size: int) -> np.ndarray:
    """|y[k] - y[k-1]| at every block boundary of a 1-D signal."""
    y = np.asarray(y, dtype=np.float64)
    if block_size < 1:
        raise ValueError("block_size must be >= 1")
    idx = np.arange(block_size, y.size, block_size)
    if idx.size == 0:
        return np.zeros(0, dtype=np.float64)
    return np.abs(y[idx] - y[idx - 1])


def find_discontinuities(y: np.ndarray, threshold: float) -> np.ndarray:
    """Indices k where |y[k] - y[k-1]| exceeds ``threshold``."""
    y = np.asarray(y, dtype=np.float64)
    if y.size < 2:
        return np.zeros(0, dtype=np.int64)
    jumps = np.abs(np.diff(y))
    return np.nonzero(jumps > threshold)[0] + 1
