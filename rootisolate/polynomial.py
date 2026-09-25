"""单变量整系数 / 有理系数多项式的精确算术。

内部表示：numpy 的 dtype=object 一维数组，下标 i 对应 x^i 的系数，
元素全部是 fractions.Fraction。NumPy 在此只承担：
  * np.convolve（多项式乘法）；
  * 逐元素加、减、标量乘、比较（对 Fraction 仍是精确整数运算）；
  * 向量化 Horner 多点求值。
不使用任何浮点路径。
"""
from __future__ import annotations

from fractions import Fraction
from math import gcd, lcm

import numpy as np

Rational = Fraction


def poly(coeffs) -> np.ndarray:
    """由可迭代对象构造已去掉高次零系数的 Fraction 系数数组。"""
    a = np.array([Fraction(c) for c in coeffs], dtype=object)
    return trim(a)


def trim(a: np.ndarray) -> np.ndarray:
    i = len(a)
    while i > 0 and a[i - 1] == 0:
        i -= 1
    return a[:i]


def is_zero(a: np.ndarray) -> bool:
    return len(a) == 0


def degree(a: np.ndarray) -> int:
    """零多项式次数按约定为 -1。"""
    return len(a) - 1


def _pad(a: np.ndarray, n: int) -> np.ndarray:
    if len(a) >= n:
        return a
    out = np.zeros(n, dtype=object)
    out[: len(a)] = a
    return out


def add(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    n = max(len(a), len(b))
    return trim(_pad(a, n) + _pad(b, n))


def neg(a: np.ndarray) -> np.ndarray:
    return -a


def sub(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    n = max(len(a), len(b))
    return trim(_pad(a, n) - _pad(b, n))


def mul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    if is_zero(a) or is_zero(b):
        return np.array([], dtype=object)
    return trim(np.convolve(a, b))


def scale(a: np.ndarray, c: Rational) -> np.ndarray:
    if c == 0:
        return np.array([], dtype=object)
    return a * Fraction(c)


def divmod_poly(a: np.ndarray, b: np.ndarray):
    """多项式带余除法，返回 (商, 余数)，b 非零。结果仍是精确 Fraction。"""
    if is_zero(b):
        raise ZeroDivisionError("polynomial division by zero")
    a = trim(a)
    b = trim(b)
    db = len(b) - 1
    if len(a) - 1 < db:
        return np.array([], dtype=object), trim(a.copy())
    r = list(a)
    dq = len(r) - 1 - db
    q = [Fraction(0)] * (dq + 1)
    lc = b[db]
    for i in range(dq, -1, -1):
        t = r[i + db] / lc
        q[i] = t
        for j in range(db + 1):
            r[i + j] -= t * b[j]
    return poly(q), trim(np.array(r, dtype=object))


def primitive_positive(a: np.ndarray):
    """把有理系数多项式化为“本原整数”表示，缩放因子恒为正数。

    返回 (g, s)，其中 g 是 int 系数数组（内容为 1，即各系数绝对值 gcd 为 1），
    s 为正有理数，满足 a = s * g（保持首项符号）。

    取正比例缩放（绝不改变符号）是 Sturm 链与无平方分解共同复用
    本函数的前提。
    """
    a = trim(a)
    if is_zero(a):
        return np.array([], dtype=object), Fraction(1)
    denoms = [c.denominator for c in a]
    D = 1
    for d in denoms:
        D = lcm(D, d)
    g_int = np.array([int(c * D) for c in a], dtype=object)
    h = 0
    for c in g_int:
        h = gcd(h, abs(int(c)))
    if h == 0:
        h = 1
    g = g_int // h
    # a = (h/D) * g，s = h/D > 0
    return g, Fraction(h, D)


def gcd_poly(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Q[x] 上的首一 GCD。输入/输出为 Fraction 数组。"""
    a, b = trim(a), trim(b)
    if is_zero(a):
        return monic(b)
    if is_zero(b):
        return monic(a)
    while not is_zero(b):
        _, r = divmod_poly(a, b)
        # 每步余数做正整数化，压制系数增长；正比例缩放不改变 GCD。
        g_int, _ = primitive_positive(r)
        a, b = b, poly(g_int)
    return monic(a)


def monic(a: np.ndarray) -> np.ndarray:
    a = trim(a)
    if is_zero(a):
        return a
    return scale(a, Fraction(1) / a[-1])


def div_exact(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    q, r = divmod_poly(a, b)
    if not is_zero(r):
        raise ValueError("polynomial division is not exact")
    return q


def derivative(a: np.ndarray) -> np.ndarray:
    if len(a) <= 1:
        return np.array([], dtype=object)
    i = np.arange(1, len(a), dtype=object)
    return trim(a[1:] * i)


def horner(a: np.ndarray, x: Rational) -> Rational:
    """单点精确求值。"""
    acc = Fraction(0)
    for c in a[::-1]:
        acc = acc * x + c
    return acc


def sign_at(a: np.ndarray, x: Rational) -> int:
    """只取 p(x) 的符号（-1/0/1），精确且不构造会引发 GCD 爆炸的 Fraction。

    令 x = n/d（d>0），p(x)=Σ (u_i/v_i) n^i/d^i。取 L = lcm(v_i)，
    c_i = u_i·(L/v_i) 为整数。则
        p(x) = (1/L) Σ c_i n^i/d^i
             = (1/(L·d^k)) Σ c_i n^i d^(k-i)
    分母恒正，故符号等于整数 S 的符号。S 用整数 Horner：
        S = (...((c_k·n + c_{k-1}·d)·n + c_{k-2}·d^2)...)·n + c_0·d^k
    递推时维护 d 的幂，全部为 Python 大整数精确运算。
    """
    a = trim(a)
    if is_zero(a):
        return 0
    n, d = x.numerator, x.denominator
    L = 1
    for c in a:
        L = lcm(L, c.denominator)
    k = len(a) - 1
    acc = int(a[k] * L)                 # c_k
    dpow = d                            # d^(k-i)，首次 i=k-1 -> d^1
    for i in range(k - 1, -1, -1):
        acc = acc * n + int(a[i] * L) * dpow
        dpow *= d
    return 1 if acc > 0 else (-1 if acc < 0 else 0)


def horner_vec(a: np.ndarray, xs) -> np.ndarray:
    """多点精确求值（向量化 Horner，object 数组上的逐元素有理运算）。"""
    acc = np.zeros(len(xs), dtype=object)
    for c in a[::-1]:
        acc = acc * xs + c
    return acc


def cauchy_bound(a: np.ndarray) -> Rational:
    """Cauchy 根界：所有根 z（含复根）满足 |z| < 1 + max_{k<n}|a_k/a_n|。

    返回整数 B，使全部实根严格落在 (-B, B)。对首一/本原情形很快。
    """
    a = trim(a)
    if len(a) <= 1:
        return Fraction(0)
    lc = abs(a[-1])
    m = max(abs(c) for c in a[:-1])
    return 1 + (m + lc - 1) // lc  # ceil(1 + m/lc)
