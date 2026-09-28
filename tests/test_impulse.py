"""Impulse tests: T @ e_j must reproduce column j, where the expected column
is read directly off c and r by the index rule (no multiplication involved)."""

import numpy as np

from fixtures.generators import impulse_cases
from toeplitz_fft import matvec


def test_every_impulse_recovers_its_column():
    cases = impulse_cases(seed=31, m=7, n=5)
    assert len(cases) == 5
    for case in cases:
        y, _ = matvec(case.c, case.r, case.x)
        np.testing.assert_allclose(y, case.expected, rtol=1e-12, atol=1e-12)


def test_impulse_zero_and_last_hand_checked():
    # c = [10, 20, 30], r = [10, 40, 50]
    # T = [[10, 40, 50], [20, 10, 40], [30, 20, 10]]
    c = np.array([10.0, 20.0, 30.0])
    r = np.array([10.0, 40.0, 50.0])
    y0, _ = matvec(c, r, np.array([1.0, 0.0, 0.0]))
    np.testing.assert_allclose(y0, [10.0, 20.0, 30.0], rtol=1e-12, atol=1e-12)
    y2, _ = matvec(c, r, np.array([0.0, 0.0, 1.0]))
    np.testing.assert_allclose(y2, [50.0, 40.0, 10.0], rtol=1e-12, atol=1e-12)
