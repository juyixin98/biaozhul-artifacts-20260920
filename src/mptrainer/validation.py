"""Numerical-validation layer: comparison helpers for tests and demo.

These functions are deliberately independent of the trainer internals so
tests can assert properties (conservation, tolerance, trajectory match)
without re-deriving them from the code under test.
"""

from __future__ import annotations

from typing import Mapping

import numpy as np


def max_abs_diff(a: Mapping[str, np.ndarray], b: Mapping[str, np.ndarray]) -> float:
    """Largest element-wise |a-b| across two identically-keyed param dicts."""
    if set(a) != set(b):
        raise ValueError(f"param keys differ: {set(a) ^ set(b)}")
    return float(max(np.max(np.abs(a[k].astype(np.float64) - b[k].astype(np.float64))) for k in a))


def max_rel_diff(a: Mapping[str, np.ndarray], b: Mapping[str, np.ndarray], eps: float = 1e-12) -> float:
    """Largest |a-b| / max(|b|, eps) across two param dicts."""
    if set(a) != set(b):
        raise ValueError(f"param keys differ: {set(a) ^ set(b)}")
    worst = 0.0
    for k in a:
        denom = np.maximum(np.abs(b[k].astype(np.float64)), eps)
        worst = max(worst, float(np.max(np.abs(a[k] - b[k]) / denom)))
    return worst


def params_bitwise_equal(a: Mapping[str, np.ndarray], b: Mapping[str, np.ndarray]) -> bool:
    """Exact bitwise equality -- the master-conservation check for skipped steps."""
    return set(a) == set(b) and all(
        a[k].dtype == b[k].dtype and np.array_equal(a[k], b[k]) for k in a
    )


def snapshot(params: Mapping[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Deep copy of a param dict for before/after comparisons."""
    return {k: v.copy() for k, v in params.items()}
