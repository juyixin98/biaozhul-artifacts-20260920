"""Real-mode kernel tests: hand values, dense cross-checks, failure categories."""

import numpy as np
import pytest

from fixtures.generators import asymmetric_real_case, batch_case, hand_case, tiny_cases
from toeplitz_fft import ToeplitzConfig, matmat, matvec
from toeplitz_fft.errors import ShapeMismatchError, UnsupportedModeError
from toeplitz_fft.reference import dense_matmat, dense_matvec

TIGHT = dict(rtol=1e-12, atol=1e-12)


def test_hand_computed_3x2_exact():
    case = hand_case()
    y, meta = matvec(case.c, case.r, case.x)
    np.testing.assert_allclose(y, [29.0, 16.0, 27.0], **TIGHT)
    assert meta["mode"] == "real"
    assert meta["dtype"] == "float64"
    assert meta["L"] == 4  # m + n - 1


def test_asymmetric_nonpow2_matches_dense_reference():
    case = asymmetric_real_case(seed=11, m=7, n=5)
    y, meta = matvec(case.c, case.r, case.x)
    expected = dense_matvec(case.c, case.r, case.x)  # independent dense path
    np.testing.assert_allclose(y, expected, **TIGHT)
    assert meta["L"] == 11  # not a power of two, no padding by default


def test_tiny_cases_go_through_fft_path_with_exact_results():
    for case in tiny_cases():
        y, meta = matvec(case.c, case.r, case.x)
        np.testing.assert_allclose(y, case.expected, **TIGHT)
        assert meta["L"] == case.c.size + case.r.size - 1


def test_batched_matmat_matches_per_column_matvec_and_dense():
    case = batch_case(seed=12, m=9, n=6, k=4)
    Y, meta = matmat(case.c, case.r, case.x)
    assert Y.shape == (9, 4)
    assert meta["k"] == 4
    np.testing.assert_allclose(Y, dense_matmat(case.c, case.r, case.x), **TIGHT)
    for j in range(4):
        yj, _ = matvec(case.c, case.r, case.x[:, j])
        np.testing.assert_allclose(Y[:, j], yj, **TIGHT)


def test_real_output_dtype_is_explicit_and_configurable():
    case = hand_case()
    y, meta = matvec(case.c, case.r, case.x,
                     config=ToeplitzConfig(real_dtype="float32", complex_dtype="complex64"))
    assert y.dtype == np.float32
    assert meta["dtype"] == "float32"


def test_wrong_vector_length_raises_shape_mismatch():
    case = hand_case()
    with pytest.raises(ShapeMismatchError) as excinfo:
        matvec(case.c, case.r, np.zeros(case.r.size + 1))
    assert excinfo.value.category == "shape_mismatch"


def test_forced_real_mode_rejects_complex_data():
    case = hand_case()
    x_complex = case.x.astype(np.complex128) * (1 + 1j)
    with pytest.raises(UnsupportedModeError) as excinfo:
        matvec(case.c, case.r, x_complex, mode="real")
    assert excinfo.value.category == "unsupported_mode"


def test_invalid_mode_raises_unsupported_mode():
    case = hand_case()
    with pytest.raises(UnsupportedModeError):
        matvec(case.c, case.r, case.x, mode="banana")
