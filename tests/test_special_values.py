"""特殊值规则测试：NaN / Inf / 带符号零 / 空输入，断言具体失败类别。"""

from __future__ import annotations

import math

import pytest

from app.kernels import Method, sum_with_policy
from app.kernels.policy import FailureCategory, SummationRejected


def _category(excinfo: pytest.ExceptionInfo) -> FailureCategory:
    return excinfo.value.category


def test_empty_input_rejected() -> None:
    with pytest.raises(SummationRejected) as excinfo:
        sum_with_policy(Method.NAIVE, [], chunk_size=8)
    assert _category(excinfo) is FailureCategory.EMPTY_INPUT


def test_nan_rejected_with_category() -> None:
    with pytest.raises(SummationRejected) as excinfo:
        sum_with_policy(Method.COMPENSATED, [1.0, float("nan"), 2.0], chunk_size=8)
    assert _category(excinfo) is FailureCategory.NON_FINITE_INPUT
    assert excinfo.value.detail["nan_count"] == 1
    # 脱敏：detail 里只有计数与摘要，没有原始序列
    assert "values" not in excinfo.value.detail


def test_mixed_infinities_rejected() -> None:
    with pytest.raises(SummationRejected) as excinfo:
        sum_with_policy(Method.PAIRWISE, [math.inf, -math.inf], chunk_size=8)
    assert _category(excinfo) is FailureCategory.MIXED_INFINITIES


def test_single_sign_infinity_passthrough() -> None:
    result, _ = sum_with_policy(Method.NAIVE, [1.0, math.inf, 2.0], chunk_size=8)
    assert result == math.inf
    result, _ = sum_with_policy(Method.COMPENSATED, [-math.inf, 1.0], chunk_size=8)
    assert result == -math.inf


def test_all_negative_zero_gives_negative_zero() -> None:
    result, _ = sum_with_policy(Method.COMPENSATED, [-0.0, -0.0, -0.0], chunk_size=8)
    assert result == 0.0 and math.copysign(1.0, result) < 0


def test_mixed_zeros_give_positive_zero() -> None:
    result, _ = sum_with_policy(Method.COMPENSATED, [-0.0, 0.0], chunk_size=8)
    assert result == 0.0 and math.copysign(1.0, result) > 0


def test_exact_cancellation_gives_positive_zero() -> None:
    # 非全零输入精确抵消到 0：固定规则为 +0.0
    result, _ = sum_with_policy(Method.COMPENSATED, [1.0, -1.0], chunk_size=8)
    assert result == 0.0 and math.copysign(1.0, result) > 0
