"""Unit tests for the precision primitives, factorizations and metrics."""

from __future__ import annotations

import numpy as np
import pytest

from app.numerical import FLOAT_EPSILON, mp, mpf, workprec
from app.numerical import arithmetic as arith
from app.numerical import error_metrics
from app.numerical.factorization import factor_binary, factor_mp


def test_workprec_restores_global_precision() -> None:
    before = mp.dps
    with workprec(33):
        assert mp.dps == 33
    assert mp.dps == before


def test_to_mp_parses_exact_decimal_strings() -> None:
    # At the default 15 dps this literal would round; parsing keeps full
    # precision once a sufficient working precision is active.
    with workprec(50):
        value = mpf("1.000000000000000000000000000001")
        assert value != mpf(1)
        delta = value - mpf(1)
        assert mpf("9.9e-31") < delta < mpf("1.01e-30")


def test_binary_factorizations_round_trip() -> None:
    # Hilbert-ish well-conditioned matrix, exact at both dtypes.
    a = mp.matrix(
        [
            [mpf(4), mpf(1), mpf(0)],
            [mpf(1), mpf(3), mpf(1)],
            [mpf(0), mpf(1), mpf(2)],
        ]
    )
    for dtype in ("float32", "float64"):
        handle = factor_binary(a, dtype)
        rhs = np.ones(3, dtype=handle.dtype)
        x = handle.solve_columns(rhs)
        a_np = np.array(
            [[float(a[i, j]) for j in range(3)] for i in range(3)],
            dtype=handle.dtype,
        )
        residual = a_np @ x - np.ones(3, dtype=handle.dtype)
        assert np.max(np.abs(residual)) < 1e-4 * FLOAT_EPSILON[dtype] ** 0.5


def test_mp_lu_solves_and_transpose_solves() -> None:
    with workprec(70):
        a = mp.matrix(
            [
                [mpf(2), mpf(1), mpf(1)],
                [mpf(1), mpf(3), mpf(2)],
                [mpf(1), mpf(2), mpf(2)],
            ]
        )
        b = [mpf(7), mpf(13), mpf(11)]
        handle = factor_mp(a, 60, mpf(100))
        x = handle.solve_column(b)
        residual = [sum(a[i, k] * x[k] for k in range(3)) - b[i] for i in range(3)]
        assert max(abs(v) for v in residual) < mpf("1e-55")

        # A^T y = x  -> verify A^T y = x independently.
        y = handle.solve_transpose_column(x)
        aty = [sum(a[k, i] * y[k] for k in range(3)) for i in range(3)]
        assert max(abs(aty[i] - x[i]) for i in range(3)) < mpf("1e-55")


def test_residual_uses_original_matrix_exactly() -> None:
    # x is the exact solution; residual must be zero to the working precision.
    with workprec(60):
        a = mp.matrix([[mpf(2), mpf(1)], [mpf(1), mpf(3)]])
        x = mp.matrix([[mpf("0.3")], [mpf("0.7")]])
        b = arith.mat_mat(a, x)
        r = arith.residual_matrix(a, x, b)
        assert r[0, 0] == 0 and r[1, 0] == 0


def test_backward_error_zero_for_exact_solution_and_positive_otherwise() -> None:
    with workprec(60):
        a = mp.matrix([[mpf(2), mpf(1)], [mpf(1), mpf(3)]])
        x = mp.matrix([[mpf(1)], [mpf(1)]])
        b = arith.mat_mat(a, x)
        reports = error_metrics.column_errors(a, x, b, arith.residual_matrix(a, x, b))
        assert reports[0].eta_normwise == 0
        assert reports[0].omega_componentwise == 0

        x_bad = mp.matrix([[mpf(1) + mpf("1e-20")], [mpf(1)]])
        r_bad = arith.residual_matrix(a, x_bad, b)
        reports_bad = error_metrics.column_errors(a, x_bad, b, r_bad)
        assert reports_bad[0].eta_normwise > 0


def test_backward_error_is_per_column() -> None:
    with workprec(60):
        a = mp.matrix([[mpf(2), mpf(0)], [mpf(0), mpf(4)]])
        x = mp.matrix(
            [[mpf(1), mpf(1) + mpf("1e-18")], [mpf(1), mpf(1)]]
        )
        b = mp.matrix([[mpf(2), mpf(2)], [mpf(4), mpf(4)]])
        r = arith.residual_matrix(a, x, b)
        reports = error_metrics.column_errors(a, x, b, r)
        assert reports[0].eta_normwise == 0
        assert reports[1].eta_normwise > 0
        assert reports[1].column == 1


@pytest.mark.parametrize("dps", [30, 60])
def test_mp_factorization_identifies_exact_singular(dps: int) -> None:
    a = mp.matrix(
        [
            [mpf(1), mpf(2), mpf(3)],
            [mpf(2), mpf(4), mpf(6)],  # 2 * row 0
            [mpf(1), mpf(0), mpf(1)],
        ]
    )
    handle = factor_mp(a, dps, mpf(100))
    assert handle.singular is True
