"""Complex-mode kernel tests: dense cross-check plus 50-digit mpmath reference."""

import numpy as np

from fixtures.generators import complex_case
from toeplitz_fft import matvec
from toeplitz_fft.reference import dense_matvec, mpmath_matvec


def test_complex_matches_dense_reference():
    case = complex_case(seed=21, m=6, n=4)
    y, meta = matvec(case.c, case.r, case.x)
    assert meta["mode"] == "complex"
    assert meta["dtype"] == "complex128"
    assert y.dtype == np.complex128
    np.testing.assert_allclose(y, dense_matvec(case.c, case.r, case.x),
                               rtol=1e-12, atol=1e-12)


def test_complex_matches_mpmath_50digit_reference():
    """mpmath reference is fully independent of both the kernel and BLAS."""
    case = complex_case(seed=22, m=4, n=3)
    y, _ = matvec(case.c, case.r, case.x)
    ref = mpmath_matvec(case.c, case.r, case.x, dps=50)
    ref_arr = np.array([complex(v) for v in ref], dtype=np.complex128)
    err = np.max(np.abs(y - ref_arr))
    assert err < 1e-12, f"FFT result deviates from 50-digit reference by {err:.3e}"


def test_forced_complex_mode_on_real_data_returns_complex():
    c = np.array([1.0, 2.0])
    r = np.array([1.0, 3.0])
    x = np.array([1.0, 1.0])
    y, meta = matvec(c, r, x, mode="complex")
    assert meta["mode"] == "complex"
    assert y.dtype == np.complex128
    np.testing.assert_allclose(y, [4.0, 3.0], rtol=1e-12, atol=1e-12)


def test_auto_mode_promotes_when_only_x_is_complex():
    c = np.array([1.0, 2.0])
    r = np.array([1.0, 3.0])
    x = np.array([1.0 + 1.0j, 0.0])
    y, meta = matvec(c, r, x)
    assert meta["mode"] == "complex"
    np.testing.assert_allclose(y, [1 + 1j, 2 + 2j], rtol=1e-12, atol=1e-12)
