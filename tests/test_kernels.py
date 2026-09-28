"""Kernel correctness tests with concrete hand-computed answers.

Reference answers here are written by hand from the Toeplitz definition or
produced by ``scipy.linalg.toeplitz`` dense multiplication - never by the
embedding FFT kernel under test.
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.linalg import toeplitz as scipy_toeplitz

from toeplitz_fft.kernels import (
    build_embedding,
    direct_multiply,
    fft_multiply_embedding,
    min_embedding_size,
    padded_embedding_size,
    toeplitz_dense,
)


# Hand-written asymmetric 3x3:
#   T = [[1, 4, 5],
#        [2, 1, 4],
#        [3, 2, 1]]
C3 = np.array([1.0, 2.0, 3.0])
R3 = np.array([1.0, 4.0, 5.0])


def test_dense_matches_handwritten_matrix() -> None:
    expected = np.array([[1.0, 4.0, 5.0],
                         [2.0, 1.0, 4.0],
                         [3.0, 2.0, 1.0]])
    np.testing.assert_array_equal(toeplitz_dense(C3, R3), expected)
    np.testing.assert_array_equal(toeplitz_dense(C3, R3),
                                  scipy_toeplitz(C3, R3))


def test_fft_matches_hand_computed_product() -> None:
    # T @ [1, 0, -1] computed by hand:
    # row0: 1 - 5 = -4
    # row1: 2 - 4 = -2
    # row2: 3 - 1 = 2
    x = np.array([[1.0, 0.0, -1.0]])
    y, stats = fft_multiply_embedding(C3, R3, x)
    np.testing.assert_allclose(y, [[-4.0, -2.0, 2.0]], rtol=1e-12, atol=1e-12)
    # 2n-1 = 5; scipy's next_fast_len(5) == 5, still non-power-of-two
    assert stats["m_minimum"] == 5
    assert stats["m"] == 5
    assert stats["m_is_power_of_two"] is False


def test_embedding_first_column_layout() -> None:
    # v = [c0, c1, c2, r2, r1] = [1, 2, 3, 5, 4] (shared c0 used once)
    v = build_embedding(C3, R3, 5)
    np.testing.assert_array_equal(v, [1.0, 2.0, 3.0, 5.0, 4.0])


def test_aliasing_bound_is_enforced() -> None:
    assert min_embedding_size(3) == 5
    with pytest.raises(ValueError):
        build_embedding(C3, R3, 4)
    with pytest.raises(ValueError):
        fft_multiply_embedding(C3, R3, np.zeros((1, 3)), m=4)


def test_n1_scalar_multiplication() -> None:
    y, _ = fft_multiply_embedding(np.array([3.5]), np.array([3.5]),
                                  np.array([[2.0], [-4.0]]))
    np.testing.assert_allclose(y, [[7.0], [-14.0]], rtol=1e-12, atol=1e-12)


@pytest.mark.parametrize("n", [1, 2, 5, 13, 17, 31, 100])
def test_real_asymmetric_non_power_of_two_vs_dense(n: int) -> None:
    rng = np.random.default_rng(100 + n)
    c = rng.standard_normal(n)
    r = rng.standard_normal(n)
    r[0] = c[0]
    # Guarantee T != T^T
    r[1:] = r[1:] + 3.0
    x = rng.standard_normal((3, n))
    y, stats = fft_multiply_embedding(c, r, x)
    expected = direct_multiply(c, r, x)
    np.testing.assert_allclose(y, expected, rtol=1e-10, atol=1e-10)
    if n >= 2:  # a 1x1 matrix is trivially symmetric
        assert not np.allclose(toeplitz_dense(c, r), toeplitz_dense(c, r).T)
    assert stats["m"] >= 2 * n - 1


def test_complex_non_hermitian_vs_scipy_dense() -> None:
    n = 17  # non power of two
    rng = np.random.default_rng(7)
    c = rng.standard_normal(n) + 1j * rng.standard_normal(n)
    r = rng.standard_normal(n) + 1j * rng.standard_normal(n)
    r[0] = c[0]
    x = rng.standard_normal((2, n)) + 1j * rng.standard_normal((2, n))
    y, _ = fft_multiply_embedding(c, r, x)
    expected = x @ scipy_toeplitz(c, r).T
    np.testing.assert_allclose(y, expected, rtol=1e-10, atol=1e-10)


def test_impulse_returns_matrix_columns() -> None:
    n = 15
    rng = np.random.default_rng(11)
    c = rng.standard_normal(n)
    r = rng.standard_normal(n)
    r[0] = c[0]
    positions = (0, 4, 14)
    x = np.zeros((len(positions), n))
    for b, k in enumerate(positions):
        x[b, k] = 1.0
    y, _ = fft_multiply_embedding(c, r, x)
    t = scipy_toeplitz(c, r)
    for b, k in enumerate(positions):
        np.testing.assert_allclose(y[b], t[:, k], rtol=1e-12, atol=1e-12)


def test_real_output_is_real_dtype() -> None:
    y, stats = fft_multiply_embedding(C3, R3, np.array([[1.0, 2.0, 3.0]]))
    assert not np.iscomplexobj(y)
    assert y.dtype == np.dtype(np.float64)
    assert stats["mode"] == "real"


def test_single_precision_output_is_float32() -> None:
    c = C3.astype(np.float32)
    r = R3.astype(np.float32)
    x = np.array([[1.0, 2.0, 3.0]], dtype=np.float32)
    y, _ = fft_multiply_embedding(c, r, x)
    assert y.dtype == np.dtype(np.float32)
    expected = (x @ scipy_toeplitz(c, r).T)
    np.testing.assert_allclose(y, expected, rtol=1e-5, atol=1e-6)


def test_embedding_size_never_shrinks_below_bound() -> None:
    for n in range(1, 80):
        assert padded_embedding_size(n) >= 2 * n - 1
