"""输入边界：数值校验、零首项规范化、零多项式拒绝、错误分类可区分。"""

from __future__ import annotations

import numpy as np
import pytest

from polyroots.errors import (
    InvalidCoefficientError,
    NonFiniteCoefficientError,
    ResourceExhaustedError,
    ZeroPolynomialError,
)
from polyroots.models import CoeffOrder
from polyroots.validation import parse_and_normalize

pytestmark = pytest.mark.unit


def test_descending_order_is_normalized_to_ascending_monic() -> None:
    # 2x^2 + 0x + 1（降序 [2,0,1]）-> 首一升序 [0.5, 0, 1]
    norm, stripped = parse_and_normalize([2, 0, 1], CoeffOrder.DESCENDING, 256)
    assert stripped == 0
    assert norm.size == 3
    assert norm[0] == pytest.approx(0.5 + 0j)
    assert norm[1] == pytest.approx(0.0 + 0j)
    assert norm[2] == pytest.approx(1.0 + 0j)


def test_ascending_order_is_preserved() -> None:
    norm, _ = parse_and_normalize([1, 0, 2], CoeffOrder.ASCENDING, 256)
    assert norm[0] == pytest.approx(0.5 + 0j)
    assert norm[-1] == pytest.approx(1.0 + 0j)


def test_leading_zero_coefficients_are_stripped_not_silently_mistreated() -> None:
    # 降序 [0,0,1,2]：前两个是零首项，真实多项式为 x+2
    norm, stripped = parse_and_normalize([0, 0, 1, 2], CoeffOrder.DESCENDING, 256)
    assert stripped == 2
    assert norm.size == 2  # 次数被正确识别为 1，而不是 3


def test_zero_polynomial_is_rejected_not_returning_nonsense() -> None:
    with pytest.raises(ZeroPolynomialError) as exc:
        parse_and_normalize([0, 0, 0], CoeffOrder.DESCENDING, 256)
    assert exc.value.code == "zero_polynomial"
    assert exc.value.category == "input_error"


def test_tiny_but_nonzero_polynomial_is_not_confused_with_zero() -> None:
    # 全部很小但非零：先按尺度判零首项，再首一化，绝不能误判为零多项式
    norm, stripped = parse_and_normalize([1e-300, 2e-300], CoeffOrder.DESCENDING, 256)
    assert stripped == 0
    assert norm[-1] == pytest.approx(1.0 + 0j)


@pytest.mark.parametrize("bad", [[], ["1", "2"], [True, 1], [None], [object()]])
def test_non_numeric_coefficients_raise_input_error(bad: list) -> None:
    with pytest.raises(InvalidCoefficientError):
        parse_and_normalize(bad, CoeffOrder.DESCENDING, 256)


@pytest.mark.parametrize("bad", [[float("nan")], [float("inf")], [[1, float("nan")]]])
def test_non_finite_coefficients_raise_distinct_error(bad: list) -> None:
    with pytest.raises(NonFiniteCoefficientError) as exc:
        parse_and_normalize(bad, CoeffOrder.DESCENDING, 256)
    assert exc.value.code == "non_finite_coefficient"
    # 必须与一般格式错误是不同 code，便于调用方区分失败类别
    assert exc.value.code != "invalid_coefficients"


def test_complex_shapes_accepted() -> None:
    a, _ = parse_and_normalize([[1, 2], [3, 4]], CoeffOrder.DESCENDING, 256)
    b, _ = parse_and_normalize([{"real": 1, "imag": 2},
                                {"real": 3, "imag": 4}], CoeffOrder.DESCENDING, 256)
    c, _ = parse_and_normalize([1 + 2j, 3 + 4j], CoeffOrder.DESCENDING, 256)
    np.testing.assert_allclose(a, b)
    np.testing.assert_allclose(a, c)


def test_pair_shape_must_have_exactly_two_elements() -> None:
    with pytest.raises(InvalidCoefficientError):
        parse_and_normalize([[1, 2, 3]], CoeffOrder.DESCENDING, 256)


def test_degree_above_limit_is_resource_not_input_error() -> None:
    # x^300 - 1，升序：[-1, 0, ..., 0, 1]（301 个系数，次数 300）
    coeffs = [-1.0] + [0.0] * 299 + [1.0]
    with pytest.raises(ResourceExhaustedError) as exc:
        parse_and_normalize(coeffs, CoeffOrder.ASCENDING, 256)
    assert exc.value.code == "resource_exhausted"
    assert exc.value.category == "resource_exhausted"
    assert exc.value.http_status == 413


def test_error_categories_are_pairwise_distinguishable() -> None:
    # 输入错误 / 资源耗尽 两类绝不能共用 code
    assert ZeroPolynomialError.code != ResourceExhaustedError.code
    assert ZeroPolynomialError.category != ResourceExhaustedError.category
