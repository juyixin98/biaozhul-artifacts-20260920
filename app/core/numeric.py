"""Numerical validation helpers shared by executor checks and tests."""

from __future__ import annotations

import hashlib
from typing import Dict, Mapping, Tuple

import numpy as np

GRAD_RTOL = 1e-8
GRAD_ATOL = 1e-8
FD_EPS = 1e-6
FD_RTOL = 1e-6
FD_ATOL = 1e-6


def fingerprint(arr: np.ndarray) -> str:
    """Stable content digest of an array (shape + dtype + raw bytes)."""

    a = np.ascontiguousarray(arr)
    h = hashlib.sha256()
    h.update(str(a.shape).encode())
    h.update(str(a.dtype).encode())
    h.update(a.tobytes())
    return h.hexdigest()


def max_abs_diff(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.max(np.abs(np.asarray(a) - np.asarray(b))))


def max_rel_diff(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    denom = np.maximum(np.abs(b), 1e-12)
    return float(np.max(np.abs(a - b) / denom))


def assert_arrays_close(
    actual: np.ndarray,
    expected: np.ndarray,
    *,
    what: str,
    rtol: float = GRAD_RTOL,
    atol: float = GRAD_ATOL,
) -> Tuple[float, float]:
    """Assert closeness; return (max_abs, max_rel) for structured logs."""

    actual = np.asarray(actual, dtype=np.float64)
    expected = np.asarray(expected, dtype=np.float64)
    if actual.shape != expected.shape:
        raise AssertionError(
            f"{what}: shape mismatch {actual.shape} vs {expected.shape}"
        )
    abs_d = max_abs_diff(actual, expected)
    rel_d = max_rel_diff(actual, expected)
    tol = atol + rtol * np.max(np.abs(expected))
    if not np.all(np.abs(actual - expected) <= tol):
        raise AssertionError(
            f"{what}: arrays differ (max_abs={abs_d:.3e}, max_rel={rel_d:.3e})"
        )
    return abs_d, rel_d


def assert_grad_map_close(
    actual: Mapping[str, np.ndarray],
    expected: Mapping[str, np.ndarray],
    *,
    rtol: float = GRAD_RTOL,
    atol: float = GRAD_ATOL,
) -> Dict[str, Tuple[float, float]]:
    if set(actual) != set(expected):
        raise AssertionError(
            f"gradient keys differ: {sorted(actual)} vs {sorted(expected)}"
        )
    report: Dict[str, Tuple[float, float]] = {}
    for key in sorted(expected):
        report[key] = assert_arrays_close(
            actual[key], expected[key], what=f"grad[{key}]", rtol=rtol, atol=atol
        )
    return report
