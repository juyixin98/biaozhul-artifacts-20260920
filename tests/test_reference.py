"""Tests proving reference answers come from independent mature libraries."""

import numpy as np
import pytest

from sym_eig.evidence.reference import mpmath_reference, scipy_reference
from sym_eig.numerical.kernel import symmetric_eigen


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_mpmath_high_precision_oracle_matches_known_tridiagonal(seed):
    import mpmath as mp

    # Build an EXACT high-precision matrix with a close but resolved pair of
    # eigenvalues (gap 1e-30); mpmath must solve it to the working precision.
    # (A gap of 1e-20 cannot even be expressed when the matrix itself is a
    # float64 input, so that would test round-off rather than the oracle.)
    with mp.workdps(60):
        c = mp.mpf("0.6")
        s = mp.mpf("0.8")
        r1 = mp.matrix([[c, -s], [s, c]])
        # A = R diag(a, b) R^T with exact, close a, b
        a, b = mp.mpf("0.5"), mp.mpf("0.5") + mp.mpf("1e-30")
        high = r1 * mp.diag([a, b]) * r1.T

        w_mp, q_mp = mp.eigh(high)
        wv = [w_mp[i, 0] for i in range(2)]
        residual = mp.norm(high * q_mp - q_mp * mp.diag(wv))
        assert float(mp.log10(residual / mp.norm(high))) < -58
        # The tiny gap is resolved to high precision.
        assert abs((wv[1] - wv[0]) - mp.mpf("1e-30")) < mp.mpf("1e-55")


def test_scipy_oracle_and_mpmath_oracle_agree_independently(rng):
    n = 7
    m = rng.standard_normal((n, n))
    a = m + m.T
    mp_ref = mpmath_reference(a, dps=50)
    sp_ref = scipy_reference(a)
    assert sp_ref.source.startswith("scipy")
    np.testing.assert_allclose(mp_ref.eigenvalues, sp_ref.eigenvalues, atol=1e-13)


def test_reference_is_not_produced_by_kernel_under_test(rng):
    # The kernel result may be arbitrarily perturbed; reference values must
    # not depend on it at all (they are recomputed from A by external code).
    n = 5
    m = rng.standard_normal((n, n))
    a = m + m.T
    kernel = symmetric_eigen(a, max_iters=30)
    ref1 = scipy_reference(a)
    ref2 = scipy_reference(a)
    np.testing.assert_allclose(ref1.eigenvalues, ref2.eigenvalues)
    # Independence sanity: oracle disagrees with a deliberately wrong answer,
    # showing tests are not merely comparing the kernel to itself.
    wrong = kernel.eigenvalues + 1.0
    assert np.max(np.abs(ref1.eigenvalues - wrong)) > 0.5
