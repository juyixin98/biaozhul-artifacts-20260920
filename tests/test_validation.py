"""输入校验测试: 对称性相对容差与各类非法输入。"""

from __future__ import annotations

import numpy as np
import pytest

from eigenservice.config import EigenConfig
from eigenservice.errors import AsymmetryError, InvalidMatrixError
from eigenservice.validation import parse_matrix

from .fixtures import mildly_asymmetric_matrix, random_symmetric


@pytest.mark.unit
def test_symmetric_matrix_accepted_and_symmetrized() -> None:
    matrix = random_symmetric(4)
    parsed = parse_matrix(matrix.tolist(), EigenConfig())
    np.testing.assert_allclose(parsed, (matrix + matrix.T) / 2)


@pytest.mark.unit
def test_asymmetry_relative_tolerance_boundary() -> None:
    config = EigenConfig(sym_tol=1e-9)

    near_sym = mildly_asymmetric_matrix(1e-10)
    parsed = parse_matrix(near_sym, config)  # 不抛异常
    assert parsed.shape == (4, 4)

    far_sym = mildly_asymmetric_matrix(1e-6)
    with pytest.raises(AsymmetryError) as exc:
        parse_matrix(far_sym, config)
    assert exc.value.code == "asymmetric_matrix"
    assert exc.value.details["relative_asymmetry"] > 1e-9
    assert exc.value.details["sym_tol"] == 1e-9


@pytest.mark.unit
def test_relaxing_tolerance_accepts_larger_mismatch() -> None:
    matrix = mildly_asymmetric_matrix(1e-6)
    loose = EigenConfig(sym_tol=1e-3)
    parsed = parse_matrix(matrix, loose)
    assert parsed.shape == (4, 4)


@pytest.mark.unit
def test_non_square_rejected() -> None:
    with pytest.raises(InvalidMatrixError) as exc:
        parse_matrix([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]], EigenConfig())
    assert exc.value.code == "invalid_matrix"
    assert exc.value.details["rows"] != exc.value.details["cols"]


@pytest.mark.unit
def test_non_finite_rejected() -> None:
    matrix = [[1.0, float("nan")], [float("nan"), 1.0]]
    with pytest.raises(InvalidMatrixError) as exc:
        parse_matrix(matrix, EigenConfig())
    assert exc.value.code == "invalid_matrix"
    assert "NaN" in exc.value.message or "Inf" in exc.value.message


@pytest.mark.unit
def test_size_limit_configurable() -> None:
    small_config = EigenConfig(max_size=3)
    with pytest.raises(InvalidMatrixError) as exc:
        parse_matrix(np.eye(4).tolist(), small_config)
    assert exc.value.code == "invalid_matrix"
    assert exc.value.details["max_size"] == 3
    parse_matrix(np.eye(3).tolist(), small_config)  # 边界允许


@pytest.mark.unit
def test_garbage_input_rejected() -> None:
    with pytest.raises(InvalidMatrixError):
        parse_matrix("not a matrix", EigenConfig())
    with pytest.raises(InvalidMatrixError):
        parse_matrix([[1.0, "x"], ["x", 1.0]], EigenConfig())


@pytest.mark.unit
def test_one_dimensional_input_rejected() -> None:
    with pytest.raises(InvalidMatrixError) as exc:
        parse_matrix([1.0, 2.0, 3.0], EigenConfig())
    assert exc.value.code == "invalid_matrix"
    assert exc.value.details["received_ndim"] == 1


@pytest.mark.unit
def test_zero_matrix_treated_as_symmetric() -> None:
    parsed = parse_matrix(np.zeros((3, 3)).tolist(), EigenConfig())
    np.testing.assert_array_equal(parsed, np.zeros((3, 3)))
