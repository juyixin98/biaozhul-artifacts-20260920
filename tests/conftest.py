"""合成夹具：所有多项式都由“已知根”在测试侧独立构造，
答案不来自被测内核。构造方式为逐次相乘 (x-r)，与生产代码的
reconstruct_from_roots 刻意分开实现（测试独立性）。
"""

from __future__ import annotations

import numpy as np
import pytest


def poly_from_roots(roots: list[complex]) -> np.ndarray:
    """由已知根构造升序系数（测试侧独立实现）。"""
    coeffs = np.array([1.0 + 0.0j])
    for r in roots:
        coeffs = np.polynomial.polynomial.polymul(
            coeffs, np.array([-complex(r), 1.0 + 0.0j])
        )
    return np.asarray(coeffs, dtype=np.complex128)


@pytest.fixture
def real_roots_poly():
    roots = [3.0, -2.0, 0.5, 7.0]
    return roots, poly_from_roots(roots)


@pytest.fixture
def complex_roots_poly():
    roots = [1 + 2j, 1 - 2j, -3 + 1j, -3 - 1j, 2.0]
    return roots, poly_from_roots(roots)


@pytest.fixture
def near_double_poly():
    """间距 1e-7 的近重根对，外加两个普通根。"""
    delta = 1e-7
    roots = [1.0, 1.0 + delta, -4.0, 0.5]
    return roots, delta, poly_from_roots(roots)


@pytest.fixture
def exact_double_poly():
    roots = [1.0, 1.0, 2.0 + 0.0j, 2.0]
    return roots, poly_from_roots(roots)


@pytest.fixture
def roots_of_unity_poly():
    n = 64
    # x^n - 1：升序系数 [-1, 0, ..., 0, 1]
    coeffs = np.zeros(n + 1, dtype=np.complex128)
    coeffs[0] = -1.0
    coeffs[-1] = 1.0
    roots = [np.exp(2j * np.pi * k / n) for k in range(n)]
    return roots, coeffs


@pytest.fixture
def zero_roots_poly():
    """x^5：全部为零根的高阶稀疏多项式。"""
    n = 5
    coeffs = np.zeros(n + 1, dtype=np.complex128)
    coeffs[-1] = 1.0
    return [0.0] * n, coeffs


@pytest.fixture
def wilkinson_like_poly():
    """Wilkinson 型：根 1..15，对系数扰动敏感的经典用例。"""
    roots = [float(k) for k in range(1, 16)]
    return roots, poly_from_roots(roots)
