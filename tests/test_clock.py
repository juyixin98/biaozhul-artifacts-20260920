"""时钟偏移估计单元测试。"""

import numpy as np
import pytest

from sensor_matcher.clock import corrected_time, estimate_offset


@pytest.mark.unit
def test_recovers_constant_offset_without_noise() -> None:
    truth = np.linspace(0.0, 10.0, 50)
    offset = -0.25
    corr = estimate_offset(truth, truth + offset)
    assert corr.offset == pytest.approx(-offset, abs=1e-12)
    # 默认双侧裁剪 10%：50 - 2*5 = 40 个点参与中位数
    assert corr.n_used == 40
    assert corr.residuals_std == pytest.approx(0.0, abs=1e-12)


@pytest.mark.unit
def test_recovers_offset_with_noise() -> None:
    rng = np.random.default_rng(0)
    truth = np.linspace(0.0, 10.0, 200)
    offset = 0.37
    a = truth + rng.normal(0.0, 0.002, 200)
    b = truth + offset + rng.normal(0.0, 0.002, 200)
    corr = estimate_offset(a, b)
    assert corr.offset == pytest.approx(-offset, abs=0.003)


@pytest.mark.unit
def test_trimmed_estimate_robust_to_outliers() -> None:
    truth = np.linspace(0.0, 10.0, 100)
    offset = 0.1
    b = truth + offset
    # 注入 10% 大离群点
    b[:10] += 5.0
    corr = estimate_offset(truth, b, trim_ratio=0.15)
    assert corr.offset == pytest.approx(-offset, abs=1e-9)


@pytest.mark.unit
def test_single_pair() -> None:
    corr = estimate_offset(np.array([1.0]), np.array([1.2]))
    assert corr.offset == pytest.approx(-0.2)
    assert corr.n_used == 1


@pytest.mark.unit
def test_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        estimate_offset(np.array([]), np.array([]))
    with pytest.raises(ValueError):
        estimate_offset(np.array([1.0, 2.0]), np.array([1.0]))
    with pytest.raises(ValueError):
        estimate_offset(np.array([1.0, 2.0]), np.array([1.0, 2.0]), trim_ratio=0.5)


@pytest.mark.unit
def test_corrected_time_identity_near_zero() -> None:
    assert corrected_time(1.23456789, 0.0) == 1.23456789
    assert corrected_time(1.0, 0.2) == pytest.approx(1.2)
