"""数值容差与双目标支配关系。

两个权重都视为非负有限数。比较采用 NumPy 风格的逐元素容差：

    tol(x, y) = atol + rtol * max(abs(x), abs(y))

由此定义：

* ``le(a, b, i)``     —— 第 i 个目标上 a <= b（含容差）；
* ``strict_lt``       —— 至少一个目标严格更优（差值超过容差）；
* ``objectives_equal`` —— 两个目标都在容差内相等；
* ``dominates(a, b)`` —— a 两目标均不劣于 b，且至少一个严格更优。

容差对称地施加在两个方向上：若 ``abs(a-b) <= tol`` 则视为该目标相等，
既不算更优也不算更差。因此当两个标签的权重在容差带内时，它们互不支配，
都会保留（保守策略，宁可多留标签也不丢掉真实的 Pareto 点）。
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

# 标签类型：(time, cost) 二元序列
Pair = Sequence[float]


def gap(value_a: float, value_b: float, atol: float, rtol: float) -> float:
    """返回 a - b（带符号），调用方根据容差判断方向。"""
    return float(value_a) - float(value_b)


def tol_for(value_a: float, value_b: float, atol: float, rtol: float) -> float:
    return atol + rtol * max(abs(float(value_a)), abs(float(value_b)))


def objective_le(
    a: Pair, b: Pair, index: int, atol: float, rtol: float
) -> bool:
    """第 index 个目标上 a <= b（容差内相等也算 <=）。"""
    d = float(a[index]) - float(b[index])
    if d <= 0.0:
        return True
    return d <= tol_for(a[index], b[index], atol, rtol)


def objective_strict_lt(
    a: Pair, b: Pair, index: int, atol: float, rtol: float
) -> bool:
    """第 index 个目标上 a 严格优于 b（差值超过容差）。"""
    d = float(b[index]) - float(a[index])  # b - a > tol 表示 a 明显更小
    return d > tol_for(a[index], b[index], atol, rtol)


def objectives_equal(
    a: Pair, b: Pair, atol: float, rtol: float
) -> bool:
    """两个目标都在容差内相等。"""
    for i in (0, 1):
        if abs(float(a[i]) - float(b[i])) > tol_for(
            a[i], b[i], atol, rtol
        ):
            return False
    return True


def dominates(a: Pair, b: Pair, atol: float, rtol: float) -> bool:
    """弱支配且至少一维严格：a <= b 两维，且某一维严格更小。"""
    le_both = True
    any_strict = False
    for i in (0, 1):
        d = float(a[i]) - float(b[i])
        tol = tol_for(a[i], b[i], atol, rtol)
        if d > tol:  # a 在该维明显更大
            le_both = False
            break
        if d < -tol:  # a 在该维明显更小
            any_strict = True
    return le_both and any_strict


def pareto_mask(
    points: np.ndarray, atol: float, rtol: float
) -> np.ndarray:
    """对 (N,2) 目标数组返回非支配点的布尔掩码（越小越优）。

    二维顺序扫描，复杂度 O(N log N)、O(N) 内存。

    按 ``time 升序、time 相同时 cost 升序`` 排序后顺序扫描：
    排在前面的点 time 不劣于当前点，因此当前点被支配当且仅当
    已扫描点中存在 cost *严格更小*（超过容差）的点。

    容差处理：把 time 差落在容差带内的相邻点视为同一“时间组”，
    组成员之间 time 不构成严格优劣，只能按 cost 严格支配；
    只有更早（time 明显更小）的组才用其最小 cost 支配当前组。
    容差带内两个目标都相等的点互不支配、全部保留。
    """
    n = len(points)
    if n == 0:
        return np.zeros(0, dtype=bool)
    pts = np.asarray(points, dtype=float)
    # np.lexsort 的最后一个键是主键：主键 time 升序，次序键 cost 升序
    order = np.lexsort((pts[:, 1], pts[:, 0]))
    ts = pts[order, 0]
    cs = pts[order, 1]

    keep_sorted = np.zeros(n, dtype=bool)
    best_cost = np.inf  # 更早时间组中的最小 cost

    i = 0
    while i < n:
        # 时间组 [i, j)：与该组首点 time 差在容差内的连续点
        group_t = ts[i]
        j = i + 1
        while j < n and abs(ts[j] - group_t) <= tol_for(
            ts[j], group_t, atol, rtol
        ):
            j += 1

        # 组内已按 cost 升序；group_best 是组内最小 cost
        group_best = float(cs[i])
        for k in range(i, j):
            c = float(cs[k])
            # 1) 更早时间组的点 time 已 *严格更小*，因此只要其最小 cost
            #    不劣于当前 cost（当前点没有在容差外明显更省），即构成支配：
            #    不被支配要求 group_best 比 c 明显更大（差值超过容差）。
            if np.isfinite(best_cost) and not (
                best_cost - c > tol_for(best_cost, c, atol, rtol)
            ):
                continue
            # 2) 同一时间组内 time 无严格差异，严格性只能来自 cost：
            #    组内已有 cost 严格更小的点则被支配。
            if c - group_best > tol_for(c, group_best, atol, rtol):
                continue
            keep_sorted[k] = True

        best_cost = min(best_cost, group_best)
        i = j

    keep = np.zeros(n, dtype=bool)
    keep[order] = keep_sorted
    return keep
