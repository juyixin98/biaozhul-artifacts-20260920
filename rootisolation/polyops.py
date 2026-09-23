"""精确有理数多项式运算（核心算法，全部基于 fractions.Fraction）。

本模块不使用任何浮点运算，以保证符号精确：

- 多项式以系数列表 ``[a0, a1, ..., an]`` 表示 ``a0 + a1 x + ... + an x^n``；
- 多项式欧几里得算法（gcd）:func:`poly_gcd`；
- 无平方因子分解 :func:`square_free_factorization`，用于识别重根与重数；
- Sturm 序列 :func:`sturm_sequence` 及符号变化计数 :func:`sign_variations`。
"""

from __future__ import annotations

from fractions import Fraction

Poly = list[Fraction]


# ---------------------------------------------------------------------------
# 基础操作
# ---------------------------------------------------------------------------
def trim(p: Poly) -> Poly:
    """去掉高次零系数。"""
    q = list(p)
    while len(q) > 1 and q[-1] == 0:
        q.pop()
    return q


def degree(p: Poly) -> int:
    """多项式次数（零多项式约定为 -1，与代数惯例一致）。"""
    p = trim(p)
    return len(p) - 1 if p != [Fraction(0)] else -1


def is_zero(p: Poly) -> bool:
    return all(c == 0 for c in p)


def poly_add(p: Poly, q: Poly) -> Poly:
    n = max(len(p), len(q))
    return trim([(p[i] if i < len(p) else 0) + (q[i] if i < len(q) else 0)
                 for i in range(n)])


def poly_sub(p: Poly, q: Poly) -> Poly:
    n = max(len(p), len(q))
    return trim([(p[i] if i < len(p) else 0) - (q[i] if i < len(q) else 0)
                 for i in range(n)])


def poly_mul(p: Poly, q: Poly) -> Poly:
    if is_zero(p) or is_zero(q):
        return [Fraction(0)]
    r = [Fraction(0)] * (len(p) + len(q) - 1)
    for i, a in enumerate(p):
        if a == 0:
            continue
        for j, b in enumerate(q):
            r[i + j] += a * b
    return trim(r)


def poly_divmod(p: Poly, q: Poly) -> tuple[Poly, Poly]:
    """精确多项式带余除法，返回 (商, 余数)。要求 q 非零。"""
    p, q = trim(p), trim(q)
    if is_zero(q):
        raise ZeroDivisionError("多项式除零")
    if degree(p) < degree(q):
        return [Fraction(0)], p
    dp, dq = len(p) - 1, len(q) - 1
    rem = list(p)
    quot = [Fraction(0)] * (dp - dq + 1)
    lc = q[-1]
    for i in range(dp - dq, -1, -1):
        c = rem[i + dq] / lc
        if c != 0:
            quot[i] = c
            for j in range(dq + 1):
                rem[i + j] -= c * q[j]
    return trim(quot), trim(rem)


def poly_div(p: Poly, q: Poly) -> Poly:
    """精确整除；除不尽时抛出 ArithmeticError（调用方应保证可整除）。"""
    quot, rem = poly_divmod(p, q)
    if not is_zero(rem):
        raise ArithmeticError("多项式不能精确整除")
    return quot


def poly_gcd(p: Poly, q: Poly) -> Poly:
    """多项式最大公因式（首一，monic）。零多项式与另一多项式的 gcd 是另一多项式的首一化。"""
    p, q = trim(p), trim(q)
    if is_zero(p):
        return _monic(q)
    if is_zero(q):
        return _monic(p)
    while not is_zero(q):
        _, r = poly_divmod(p, q)
        p, q = q, r
    return _monic(p)


def _monic(p: Poly) -> Poly:
    p = trim(p)
    if is_zero(p) or p[-1] == 1:
        return p
    lc = p[-1]
    return [c / lc for c in p]


def _monic_positive(p: Poly) -> Poly:
    """除以首项系数的绝对值，使首项系数为 +1。

    与 :func:`_monic` 不同，这里只允许乘正的常数，因此保持多项式每一点的
    符号不变——这是 Sturm 链保持有效所必需的。
    """
    p = trim(p)
    if is_zero(p) or p[-1] == 1:
        return p
    scale = abs(p[-1])
    return [c / scale for c in p]


def derivative(p: Poly) -> Poly:
    if len(p) <= 1:
        return [Fraction(0)]
    return trim([i * p[i] for i in range(1, len(p))])


def evaluate(p: Poly, x: Fraction) -> Fraction:
    """Horner 法精确求值。"""
    r = Fraction(0)
    for c in reversed(p):
        r = r * x + c
    return r


# ---------------------------------------------------------------------------
# 无平方因子分解（square-free factorization）
# ---------------------------------------------------------------------------
def square_free_factorization(p: Poly) -> list[tuple[Poly, int]]:
    """Yun 风格的无平方因子分解，返回 ``[(g1, m1), (g2, m2), ...]``。

    保证（在精确有理数域上）``p = lc(p) · ∏ gi^mi``，其中每个 ``gi`` 首一、
    两两互素且无平方因子；``mi`` 为该因子中每个根的重数。

    采用经典迭代式：设 ``c = gcd(p, p')``，w = p/c（不同根部分），
    随后 ``y = gcd(w, c)``、``z = w/y``，逐层剥离重数为 1、2、… 的因子。
    """
    p = trim(p)
    if degree(p) <= 0:
        return []
    if is_zero(p):
        raise ValueError("零多项式无无平方因子分解")

    dp = derivative(p)
    c = poly_gcd(p, dp)                       # 含每个重根，重数 m-1
    w = poly_div(p, [p[-1]])                  # 首一化的 p，保证除法精确
    w = poly_div(w, c)                        # 不同根部分 ∏ gi
    factors: list[tuple[Poly, int]] = []
    m = 1
    while degree(w) > 0:
        y = poly_gcd(w, c)                    # 出现在剩余 c 中的根 → 重数 > m
        z = poly_div(w, y)                    # 恰为重数 m 的因子之积
        if degree(z) > 0:
            factors.append((z, m))
        w = y
        c = poly_div(c, y)
        m += 1
    # c 在循环结束时应为常数；若不是说明实现有误（数值全部精确，不会发生）
    return factors


# ---------------------------------------------------------------------------
# Sturm 序列与符号变化
# ---------------------------------------------------------------------------
def sturm_sequence(p: Poly) -> list[Poly]:
    """构造 p 的 Sturm 序列 ``[p0, p1, ..., pk]``（精确有理数）。

    取 ``p0 = p / |lc(p)|``（仅初始多项式做正归一化，压低首项系数且不
    改变符号）、``p1 = p0'``，之后严格取
    ``p_{i+1} = -(p_{i-1} mod p_i)`` 的**原始负余数，不做任何归一化**。
    关键：负余数的整体正负号是 Sturm 链有效性的一部分，若除以首项系数
    绝对值可能将其翻号，破坏“在序列多项式根处两侧项异号”这一性质。
    要求 p 无平方因子（隔离算法对每个无平方因子分量调用）。
    """
    p0 = _monic_positive(trim(p))
    seq = [p0]
    p1 = derivative(p0)
    if is_zero(p1):
        raise ValueError("Sturm 序列要求非常数多项式")
    seq.append(p1)
    while degree(seq[-1]) > 0:
        _, r = poly_divmod(seq[-2], seq[-1])
        nxt = trim([-c for c in r])          # 原始负余数，禁止归一化/翻号
        if is_zero(nxt):
            break
        seq.append(nxt)
    return seq


def sign_variations(seq: list[Poly], x: Fraction) -> int:
    """Sturm 序列在 x 处的符号变化数 V(x)。

    依 Sturm 定理，求值为 0 的项直接丢弃，再统计相邻非零值的异号次数。
    端点恰为根时，按“开区间内根数” V(a)-V(b) 自动得到正确计数。
    """
    signs: list[int] = []
    for f in seq:
        v = evaluate(f, x)
        if v > 0:
            signs.append(1)
        elif v < 0:
            signs.append(-1)
        # v == 0：丢弃
    return sum(1 for i in range(1, len(signs)) if signs[i - 1] != signs[i])


def count_distinct_roots(seq: list[Poly], a: Fraction, b: Fraction) -> int:
    """开区间 (a, b) 内的不同实根数（seq 必须无平方因子）。"""
    return sign_variations(seq, a) - sign_variations(seq, b)


# ---------------------------------------------------------------------------
# 有理根定理（Rational Root Theorem）精确有理根提取
# ---------------------------------------------------------------------------
def _divisors(n: int) -> list[int]:
    """正整数 n 的全部正约数（含 1 与 n）。"""
    n = abs(n)
    if n == 0:
        return [1]
    ds: set[int] = set()
    i = 1
    while i * i <= n:
        if n % i == 0:
            ds.add(i)
            ds.add(n // i)
        i += 1
    return sorted(ds)


def rational_linear_roots(
    p: Poly, max_candidates: int = 20_000
) -> tuple[list[Fraction], Poly]:
    """用有理根定理找出无平方因子 p 的全部**有理根**，并返回剩余多项式。

    返回 ``(roots, q)``：roots 为精确有理根（升序、每个一次），q 为除掉
    全部有理一次因子后的**首一**多项式，满足 ``p = lc(p)·∏(x-r)·q``。

    做法：把首一化的有理系数多项式通分成整系数多项式
    ``c_n x^n + ... + c_0``。对既约候选 r = u/v（u|c_0、v|c_n），代入
    **原始**多项式精确求值，命中则用综合除法除去 (x-r)。候选总数超过
    ``max_candidates`` 时放弃 RRT 优化（剩余根由 Sturm 隔离，不影响正确性）。
    """
    p = trim(p)
    if degree(p) == 1:
        return [-p[0] / p[1]], [Fraction(1)]
    if degree(p) <= 0:
        return [], p

    monic = _monic(p)
    from math import gcd
    den_lcm = 1
    for c in monic:
        den_lcm = den_lcm * c.denominator // gcd(den_lcm, c.denominator)
    intcoeffs = [int(c * den_lcm) for c in monic]
    c0, cn = intcoeffs[0], intcoeffs[-1]

    # 常数项为 0：根 0，提出 x 后递归
    if c0 == 0:
        sub_roots, sub_rest = rational_linear_roots(monic[1:], max_candidates)
        return [Fraction(0)] + sub_roots, sub_rest

    u_divs = _divisors(c0)
    v_divs = _divisors(cn)
    if len(u_divs) * len(v_divs) * 2 > max_candidates:
        return [], monic

    roots: list[Fraction] = []
    q = monic
    seen: set[Fraction] = set()
    for u in u_divs:
        for v in v_divs:
            for sgn in (1, -1):
                r = Fraction(sgn * u, v)
                if r in seen:
                    continue
                seen.add(r)
                if evaluate(q, r) == 0:
                    roots.append(r)
                    q, rem = poly_divmod(q, [-r, 1])
                    if not is_zero(rem):
                        raise AssertionError("RRT 因子除法应整除")
    return sorted(roots), trim(q)
