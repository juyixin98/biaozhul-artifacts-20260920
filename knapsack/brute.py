"""暴力枚举求解器，仅用于小规模验证（验收对照）。

直接枚举全部 2^n 个子集，取容量约束下的最大价值。
仅用于测试与验收脚本，n 超过 BRUTE_FORCE_MAX_N 时拒绝运行。
"""

from __future__ import annotations

from typing import List, Tuple

BRUTE_FORCE_MAX_N = 22


def brute_force(weights, values, capacity: int) -> Tuple[int, List[int]]:
    """返回 (最优值, 被选物品下标列表)。空集恒可行，故最优值 >= 0。"""
    weights = list(weights)
    values = list(values)
    n = len(weights)
    if n > BRUTE_FORCE_MAX_N:
        raise ValueError(f"brute force limited to n <= {BRUTE_FORCE_MAX_N}, got {n}")
    best_value = 0
    best_indices: List[int] = []
    for mask in range(1 << n):
        total_w = 0
        total_v = 0
        for i in range(n):
            if mask >> i & 1:
                total_w += weights[i]
                total_v += values[i]
                if total_w > capacity:
                    break
        else:
            if total_v > best_value:
                best_value = total_v
                best_indices = [i for i in range(n) if mask >> i & 1]
    return best_value, best_indices
