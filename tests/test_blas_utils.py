"""Tests for the BLAS-backed absolute-sum primitive."""
from __future__ import annotations

import math

import numpy as np

from app.core import blas_utils


def test_sum_abs_basic_and_agrees_with_numpy():
    values = np.array([1.0, -2.5, 3.0, 0.0, -0.25])
    assert blas_utils.sum_abs(values) == 6.75


def test_sum_abs_ignores_non_finite_without_propagating():
    values = np.array([1.0, math.inf, -2.0, math.nan])
    # Only the finite elements contribute; specials are handled elsewhere.
    assert blas_utils.sum_abs(values) == 3.0


def test_sum_abs_empty_is_zero():
    assert blas_utils.sum_abs(np.array([], dtype=np.float64)) == 0.0


def test_sum_abs_matches_numpy_on_large_random_input():
    rng = np.random.default_rng(123)
    values = rng.standard_normal(50_000)
    expected = float(np.sum(np.abs(values)))
    # BLAS dasum is a sequential reduction; NumPy's is vectorised/pairwise,
    # so the two legitimately differ by a few ulps (observed 2 here).
    assert math.isclose(
        blas_utils.sum_abs(values), expected, rel_tol=0.0,
        abs_tol=4.0 * math.ulp(expected),
    )


def test_blas_backend_is_available_in_this_environment():
    # Documents the deployment assumption: scipy BLAS is present.
    assert blas_utils._HAVE_BLAS is True
