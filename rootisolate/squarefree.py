"""无平方分解（square-free factorization）——Yun 算法，Q[x] 上精确实现。

把多项式分解为
        f = c * ∏_i f_i^i
其中每个 f_i 无平方因子（与其导数互素），且 i != j 时 gcd(f_i, f_j) = 1。
根的重数即为该根所在因子的指标 i。

先把输入化为正本原整数多项式（去内容，符号并入常数 c），
随后在 Q[x] 中用精确 GCD 迭代，全部为有理数运算。
"""
from __future__ import annotations

from fractions import Fraction

import numpy as np

from . import polynomial as P


def squarefree(a: np.ndarray):
    """返回 (factors, leading) ：factors 为 [(factor, mult), ...]，
    leading 为有理数常数 c，满足 leading * ∏ factor^mult == 原多项式。
    factors 按次数排序。
    """
    a = P.trim(a)
    if P.is_zero(a):
        return [], Fraction(0)

    # 去内容；primitive_positive 保持首项符号，a = s*g（s 为正常数）
    g_int, s = P.primitive_positive(a)
    g = P.poly(g_int)
    c = s  # a = c * g，g 的首项符号与 a 相同

    if P.degree(g) == 0:
        # 常数多项式：没有根，只保留常数
        return [], g[0] * c

    gp = P.derivative(g)
    w = P.gcd_poly(g, gp)
    y = P.div_exact(g, w)
    z = P.div_exact(gp, w)

    factors = []
    k = 1
    while P.degree(y) > 0:
        # Yun：y_k = gcd(y, z - y')；y <- y/y_k；z <- (z-y')/y_k
        zminus = P.sub(z, P.derivative(y))
        yk = P.gcd_poly(y, zminus)
        y = P.div_exact(y, yk)
        z = P.div_exact(zminus, yk)
        if P.degree(yk) > 0:
            factors.append((P.monic(yk), k))
        k += 1

    # 一致性校验：重乘应等于本原部分 g
    prod = np.array([Fraction(1)], dtype=object)
    for f, m in factors:
        fm = f
        for _ in range(m - 1):
            fm = P.mul(fm, f)
        prod = P.mul(prod, fm)
    if not _coeff_equal(P.trim(prod), P.monic(g)):
        raise RuntimeError("square-free factorization consistency check failed")

    lc = g[-1]
    factors.sort(key=lambda fm: P.degree(fm[0]))
    return factors, lc * c


def _coeff_equal(a: np.ndarray, b: np.ndarray) -> bool:
    if len(a) != len(b):
        return False
    return all(x == y for x, y in zip(a, b))
