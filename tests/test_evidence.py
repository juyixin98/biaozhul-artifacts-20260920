"""误差证据单元测试：残差尺度、聚类、敏感性、重构、Vieta。"""

from __future__ import annotations
import numpy as np
import pytest
from polyroots import evidence

pytestmark = pytest.mark.unit


def _monic_from_roots(roots: list[complex]) -> np.ndarray:
    poly = np.array([1.0 + 0j])
    for r in roots:
        poly = np.convolve(poly, np.array([-complex(r), 1.0 + 0j]))
    return poly


def test_residual_of_exact_root_is_machine_precision_level() -> None:
    c = _monic_from_roots([2.0, -3.0, 0.5])
    for z in (2.0, -3.0, 0.5):
        assert evidence.relative_residual(c, z) < 1e-14


def test_residual_is_scale_invariant_for_large_coefficients() -> None:
    # 把多项式整体放大 1e12（不改变根），首一化后相对残差应几乎相同，
    # 防止“大系数小残差”的假精度
    base = _monic_from_roots([1.0, 2.0, 3.0])
    big = base * 1e12
    big = big / big[-1]
    z = 2.0
    assert evidence.relative_residual(base, z) == pytest.approx(
        evidence.relative_residual(big, z), rel=1e-12
    )


def test_cluster_groups_only_close_roots() -> None:
    roots = np.array([1.0 + 0j, 1.0 + 1e-9j, 5.0 + 0j, 5.0 + 2e-9j, 10.0 + 0j])
    clusters = evidence.cluster_roots(roots, cluster_tol=1e-6)
    assert clusters == [[0, 1], [2, 3]]  # 第 4 个孤立根不入簇


def test_nearest_separation_is_small_for_cluster_large_otherwise() -> None:
    roots = np.array([1.0 + 0j, 1.0 + 1e-8j, 100.0 + 0j])
    sep = evidence.nearest_separation(roots)
    assert sep[0] < 1e-7
    assert sep[1] < 1e-7
    assert sep[2] > 0.5


def test_sensitivity_huge_for_near_double_moderate_for_simple() -> None:
    c = _monic_from_roots([1.0, 1.0 + 1e-7, -4.0, 0.5])
    roots = np.array([1.0 + 0j, 1.0 + 1e-7j + 0j, -4.0 + 0j, 0.5 + 0j])
    kappa = evidence.sensitivity(c, roots)
    assert kappa[0] > 1e6
    assert kappa[1] > 1e6
    assert kappa[2] < 1e3
    assert kappa[3] < 1e3


def test_vieta_relations_for_known_polynomial() -> None:
    roots = [2.0, -3.0, 0.5, 7.0]
    c = _monic_from_roots(roots)
    v = evidence.vieta_check(c, np.array(roots, dtype=complex))
    assert v.sum_rel_error < 1e-14
    assert v.product_rel_error < 1e-14


def test_vieta_detects_wrong_roots() -> None:
    # 正确多项式 (x-1)(x-2)(x-3)，但根被整体搞错
    c = _monic_from_roots([1.0, 2.0, 3.0])
    v = evidence.vieta_check(c, np.array([1.0, 2.0, 9.0]))
    assert v.sum_rel_error > 1e-2


def test_factor_reconstruction_exact_for_simple_polynomial() -> None:
    c = _monic_from_roots([1 + 2j, 3 - 1j, -2.0])
    roots = np.array([1 + 2j, 3 - 1j, -2.0])
    fe = evidence.factor_error(c, roots, high_precision=False)
    assert fe.max_rel_coeff_error < 1e-14


def test_factor_reconstruction_sparse_cancellation_is_diagnosed() -> None:
    # x^n - 1（n=64）：float64 严格重构会在零系数上产生巨大“误差”，
    # 但包络归一化判据必须保持在机器精度，且高精度复核确认是消去假象
    n = 64
    roots = np.exp(2j * np.pi * np.arange(n) / n)
    c = np.zeros(n + 1, dtype=complex)
    c[0] = -1
    c[-1] = 1
    fe = evidence.factor_error(c, roots, high_precision=True)
    assert fe.strict_float64_error > 1e-4       # 消去确实发生
    assert fe.max_rel_coeff_error < 1e-12       # 稳健判据不被骗
    assert fe.cancellation_ratio > 1e6
    assert fe.high_precision_error is not None
    assert fe.high_precision_error < 1e-10      # 独立高精度算术证实根没问题
