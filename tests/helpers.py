"""测试辅助：构造多项式、独立根数交叉验证（NumPy 浮点）。

这里刻意使用与被测实现**完全独立**的方法（NumPy 特征值求根 + 符号
包围），用于交叉核对实根个数与位置，而不是用被测代码自己证明自己。
"""

from __future__ import annotations

from fractions import Fraction

import numpy as np

from rootisolation.polyops import Poly, evaluate, trim


def poly_from_roots(roots, leading: Fraction = Fraction(1)) -> Poly:
    """由根列表生成 leading · ∏(x - r) 的系数（r 可用 Fraction 或 int）。"""
    p: Poly = [Fraction(leading)]
    for r in roots:
        r = Fraction(r)
        newp = [Fraction(0)] * (len(p) + 1)
        for i, c in enumerate(p):
            newp[i] += -r * c
            newp[i + 1] += c
        p = newp
    return trim(p)


def numpy_real_roots(coeffs_low_to_high, imag_tol: float = 1e-6,
                     dedupe: bool = True, dedupe_tol: float = 1e-5) -> list[float]:
    """用 NumPy 对多项式的友矩阵求特征值，返回数值实根（升序）。

    coeffs 为低次到高次。虚部绝对值 < imag_tol*max(1,|root|) 视为实根。
    ``dedupe=True`` 时合并重根（重根在数值上可能分裂成虚部非零的簇，
    故用稍宽的容差），返回互异实根；否则返回含重数的全部根。
    结果仅用于交叉核对，精度有限（测试留足容差）。
    """
    c = [float(x) for x in coeffs_low_to_high]
    c = list(np.trim_zeros(np.array(c), trim="b"))
    if len(c) <= 1:
        return []
    # np.roots 接受高次到低次
    roots = np.roots(list(reversed(c)))
    real = []
    for z in roots:
        if abs(z.imag) < imag_tol * max(1.0, abs(z.real)):
            real.append(float(z.real))
    real.sort()
    if not dedupe:
        return real
    distinct: list[float] = []
    for r in real:
        if not distinct or abs(r - distinct[-1]) > dedupe_tol:
            distinct.append(r)
        # 落在 dedupe_tol 内视为同一互异根
    return distinct


def count_roots_in(coeffs, lo: Fraction, hi: Fraction,
                   n_sub: int = 4001, span: float = 20.0) -> int:
    """独立的“区间内实根数”参考：在包围网格上统计符号穿越。

    仅用于测试中验证隔离区间的内部根数。对精确有理根，用精确求值补充。
    lo、hi 通常是被测区间端点；这里改用精确 evaluate 在更细的有理网格上
    数符号变化，避免浮点误判。
    """
    p = trim([Fraction(x) for x in coeffs])
    # 精确求值 + 自适应：直接对 [lo,hi] 用 Sturm 会循环依赖，故采用网格符号法，
    # 网格取在端点之间的等距分数点。
    a, b = Fraction(lo), Fraction(hi)
    prev = evaluate(p, a)
    count = 0
    grid = n_sub
    for i in range(1, grid + 1):
        x = a + (b - a) * Fraction(i, grid)
        v = evaluate(p, x)
        if v == 0:
            # 落在网格点上的根（右端点除外，由调用方语义决定）
            if i < grid:
                count += 1
            # 下一次比较用零右侧的符号：跳过该点，prev 置为下一个非零点
            prev = None
            continue
        if prev is not None and prev != 0 and ((prev < 0 < v) or (v < 0 < prev)):
            count += 1
        prev = v
    return count
