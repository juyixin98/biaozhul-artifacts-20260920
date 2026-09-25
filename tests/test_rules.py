"""Gauss-Kronrod 7-15 规则常数与单次求积精度测试。"""

import numpy as np
import pytest

from adaptive_integration.rules import (
    gk15_rule,
    _XK,
    _WK,
    _WG,
    _WG_IDX,
)


def test_nodes_symmetric_and_in_open_interval():
    assert np.allclose(_XK, -_XK[::-1])
    assert np.all(np.abs(_XK) < 1.0)
    assert np.isclose(_XK[7], 0.0, atol=1e-16)


def test_weights_symmetric_and_sums_to_two():
    # 对 [-1,1] 积分常数 1 应得 2
    assert np.isclose(np.sum(_WK), 2.0, atol=1e-14)
    assert np.allclose(_WK, _WK[::-1])
    # Gauss 7 点同样积分常数得 2
    wg = np.zeros(15)
    wg[list(_WG_IDX)] = _WG[list(_WG_IDX)]
    assert np.isclose(np.sum(wg), 2.0, atol=1e-14)


@pytest.mark.parametrize("degree", list(range(0, 22)))
def test_kronrod_exact_for_polynomials_up_to_degree_21(degree):
    # 随机系数多项式，解析积分
    rng = np.random.default_rng(42 + degree)
    c = rng.normal(size=degree + 1)

    def p(x):
        return sum(c[k] * x**k for k in range(degree + 1))

    exact = sum(c[k] / (k + 1) for k in range(degree + 1))
    r, err, _, _, floor_hit = gk15_rule(p, 0.0, 1.0)
    # 多项式精确时 K 与 G 的差值只含机器舍入
    assert abs(r - exact) <= 1e-12 * max(1.0, abs(exact))


@pytest.mark.parametrize("degree", list(range(0, 14)))
def test_embedded_gauss_exact_up_to_degree_13(degree):
    rng = np.random.default_rng(7 + degree)
    c = rng.normal(size=degree + 1)

    def p(x):
        return sum(c[k] * x**k for k in range(degree + 1))

    x = 0.5 + 0.5 * _XK
    fv = p(x)
    r_gauss = 0.5 * np.sum(_WG * fv)
    exact = sum(c[k] / (k + 1) for k in range(degree + 1))
    assert abs(r_gauss - exact) <= 1e-12 * max(1.0, abs(exact))


def test_rule_on_pi_integrand():
    r, err, _, _, _ = gk15_rule(lambda x: 4.0 / (1.0 + x**2), 0.0, 1.0)
    assert abs(r - np.pi) < 3e-9  # 单面板粗近似
    assert err > abs(r - np.pi)   # 估计应覆盖真实误差


def test_error_estimate_nonnegative():
    rng = np.random.default_rng(0)
    for a, b in [(-1, 1), (0, 5), (-3, -1)]:
        _, err, _, _, _ = gk15_rule(
            lambda x: np.sin(3 * x) + rng.normal(scale=1e-6, size=x.shape),
            float(a), float(b),
        )
        assert err >= 0.0
