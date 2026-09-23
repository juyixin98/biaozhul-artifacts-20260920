"""一维多项式实根隔离（Sturm + 二分）与区间二分细化。

全部基于精确有理数 :class:`fractions.Fraction`，区间端点始终是精确分数，
因此隔离与细化的根数结论是**精确**的，不依赖浮点容差。

失败状态以异常形式显式上报（见 :class:`IsolationLimitError`、
:class:`RefinementLimitError`），由 JSON 接口层转成 ``status != "ok"``。
"""

from __future__ import annotations

from fractions import Fraction

from .polyops import (
    Poly,
    degree,
    evaluate,
    sign_variations,
    sturm_sequence,
    trim,
)

# ---------------------------------------------------------------------------
# 失败状态
# ---------------------------------------------------------------------------


class IsolationLimitError(RuntimeError):
    """二分隔离达到最大深度仍未能分开全部根。

    ``unresolved`` 记录尚未能隔离的 (a, b, 估计根数)，``found`` 记录
    已确认的区间，便于调用方如实报告“未完成”而不是给出错误结论。
    """

    def __init__(self, depth: int, unresolved: list, found: list):
        super().__init__(f"隔离达到最大二分深度 {depth}，仍有根未能分开")
        self.depth = depth
        self.unresolved = unresolved
        self.found = found


class RefinementLimitError(RuntimeError):
    """细化达到最大迭代次数，区间仍未窄于目标宽度（拒绝假精度）。"""

    def __init__(self, iterations: int, width: Fraction, target: Fraction):
        super().__init__(
            f"细化达到最大迭代次数 {iterations}，区间宽度 {width} > 目标 {target}"
        )
        self.iterations = iterations
        self.width = width
        self.target = target


# ---------------------------------------------------------------------------
# 根界（Cauchy bound）
# ---------------------------------------------------------------------------
def cauchy_bound(p: Poly) -> Fraction:
    """Cauchy 根界 B：所有实根（及复根）严格满足 |x| < B。

    ``B = 1 + max_{k<n} |a_k / a_n|``。对无平方因子 p 而言端点 ±B 不可能
    是根；若遇到（例如无平方因子分解后理论上不会发生），翻倍直到安全。
    """
    p = trim(p)
    n = degree(p)
    lc = abs(p[-1])
    b = Fraction(1)
    for k in range(n):
        cand = abs(p[k]) / lc + 1
        if cand > b:
            b = cand
    # 防御性：确保端点不是根
    while evaluate(p, b) == 0 or evaluate(p, -b) == 0:
        b *= 2
    return b


# ---------------------------------------------------------------------------
# 隔离
# ---------------------------------------------------------------------------
def isolate_real_roots(
    p: Poly,
    max_depth: int = 10000,
) -> list[tuple[Fraction, Fraction]]:
    """对**无平方因子**多项式 p 隔离全部互异实根。

    返回每个根一个区间，互不相交、按位置排序，形如 ``(a, b)``：

    - ``a == b``：精确有理根；
    - ``a < b``：严格开区间 (a,b) 内**恰有一个根**，且两端点都不是根。

    设计（不做任何端点“内缩”）：分割只在中点进行。中点 m 是根时，把它
    作为精确点登记，并对左右两半继续按**严格内部**计数。栈中区间允许其
    右端点是根（此时它已作为精确点登记），我们压栈时把这样的右端点替换
    为一个保证非根且不丢根的点——实现上更简单的做法是：只压入端点非根
    的子区间；对端点为根的一侧，改为对该端点与 midpoint 之间的下一个
    二分点细分。

    为彻底避免脆弱的端点挪动，这里采用递归“剥离精确端点根”：每次处理
    区间 [a,b]（端点可根），先在中点切；用严格内部计数驱动，所有精确根
    在命中时立即记录，开区间叶子两端必非根。
    """
    p = trim(p)
    if degree(p) <= 0:
        return []
    seq = sturm_sequence(p)
    bound = cauchy_bound(p)

    def inner_count(a: Fraction, b: Fraction) -> int:
        """严格开区间 (a,b) 内不同根数；V(a)-V(b) 计数 (a,b]，扣除 b 根。"""
        n = sign_variations(seq, a) - sign_variations(seq, b)
        if evaluate(p, b) == 0:
            n -= 1
        return n

    exact: set[Fraction] = set()
    open_iv: list[tuple[Fraction, Fraction]] = []
    unresolved: list[tuple[Fraction, Fraction, int]] = []

    stack: list[tuple[Fraction, Fraction, int]] = [(-bound, bound, 0)]
    while stack:
        a, b, d = stack.pop()
        n = inner_count(a, b)
        if n == 0:
            continue
        if n == 1:
            # 严格内部恰有一根 r。若端点是（已登记的相邻）精确根，将其向
            # 内部移动到一个非根点；因 (a,b) 内仅有 r，取足够靠近根端点的
            # 点不会越过 r，新区间仍严格包围 r 且两端点非根。
            if evaluate(p, a) == 0:
                a = _nearby_nonroot(p, a, b)
            if evaluate(p, b) == 0:
                b = _nearby_nonroot(p, b, a)
            assert evaluate(p, a) != 0 and evaluate(p, b) != 0
            assert inner_count(a, b) == 1
            open_iv.append((a, b))
            continue
        if d >= max_depth:
            unresolved.append((a, b, n))
            continue
        m = (a + b) / 2
        if evaluate(p, m) == 0:
            exact.add(m)
        if inner_count(a, m) > 0:
            stack.append((a, m, d + 1))
        if inner_count(m, b) > 0:
            stack.append((m, b, d + 1))

    if unresolved:
        found = [(x, x) for x in exact] + open_iv
        raise IsolationLimitError(max_depth, unresolved, found)

    found = [(x, x) for x in exact] + open_iv
    found.sort(key=lambda t: ((t[0] + t[1]) / 2, t[0]))
    return found


# ---------------------------------------------------------------------------
# 细化（二分）
# ---------------------------------------------------------------------------
def _nearby_nonroot(
    p: Poly, root: Fraction, other: Fraction
) -> Fraction:
    """在 root（已知根）与 other（已知非根）之间，返回紧邻 root 的非根点。

    从 root 出发以 (other-root)/2^40 的极小步长向 other 移动并逐步加倍，
    第一个使 p 非零的点即返回。由于步长从远小于“root 到最近其它根距离”
    开始、且一遇非零立即返回，该点必落在最近其它根之前。无平方因子多项式
    的根孤立，有限步内终止。
    """
    direction = 1 if other > root else -1
    step = abs(other - root)
    for _ in range(40):
        step /= 2
    for _ in range(10_000):
        q = root + direction * step
        if evaluate(p, q) != 0:
            return q
        step *= 2
    raise RuntimeError("无法在精确根附近取得非根点")


def refine_interval(
    p: Poly,
    a: Fraction,
    b: Fraction,
    target_width: Fraction,
    max_iterations: int = 2000,
    seq: list[Poly] | None = None,
) -> tuple[Fraction, Fraction, int]:
    """对一个隔离区间做符号二分细化。

    前置条件（由 :func:`isolate_real_roots` 保证）：

    - 若 ``a == b``，它本身就是精确有理根，原样返回；
    - 否则严格开区间 (a,b) 内恰有一个根，且两端点都不是根。

    返回 ``(a', b', iterations)``：精确根返回退化区间 ``(m, m)``，否则
    返回宽度不超过 ``target_width`` 的非退化隔离区间。中点求值为 0 时
    立即确认精确根。全部用精确有理数求值；若达到 ``max_iterations`` 仍未
    达到目标宽度，抛 :class:`RefinementLimitError`，由上层如实标记，绝不
    把未达到的精度当作已达到（拒绝假精度）。
    """
    if a == b:
        return a, b, 0
    if not b > a:
        raise ValueError("细化区间要求 a <= b")

    if seq is None:
        seq = sturm_sequence(p)

    def inner(lo: Fraction, hi: Fraction) -> int:
        n = sign_variations(seq, lo) - sign_variations(seq, hi)
        if evaluate(p, hi) == 0:      # V(lo)-V(hi) 计数 (lo,hi]，扣右端点
            n -= 1
        return n

    it = 0
    while b - a > target_width:
        if it >= max_iterations:
            raise RefinementLimitError(max_iterations, b - a, target_width)
        m = (a + b) / 2
        if evaluate(p, m) == 0:
            return m, m, it + 1
        # 内部恰一根：用严格内部计数决定根落在哪一半（端点始终非根）
        if inner(a, m) == 1:
            b = m
        else:
            a = m
        it += 1
    return a, b, it


def target_width_for_digits(decimal_digits: int) -> Fraction:
    """要保证给出 d 位正确十进制小数所需的区间宽度上界 10^-d。

    约定：区间宽度 <= 10^-d 时，其中点四舍五入到 d 位小数是可靠的
    （上下界在 d 位小数处或一致、或仅差最后一个单位）。
    """
    if decimal_digits < 0:
        raise ValueError("decimal_digits 必须非负")
    if decimal_digits == 0:
        return Fraction(1)
    return Fraction(1, 10 ** decimal_digits)
