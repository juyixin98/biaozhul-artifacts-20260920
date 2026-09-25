"""小规模暴力枚举参考实现（仅供测试交叉验证）。

枚举全部 2^n 个子集（建议 n <= 18），用 NumPy 向量化位掩码与
cumsum 计算每个子集的总重量/总价值。累加器使用 Python 任意精度
整数（逐元素），避免 int64 溢出污染参考真值。
"""

from __future__ import annotations

import numpy as np


def brute_force_optimal(
    weights: list[int],
    values: list[int],
    capacity: int,
) -> tuple[int, int]:
    """返回 (最优总价值, 达到最优的子集个数)。"""

    n = len(weights)
    total = 1 << n
    best = -(10**100)
    best_count = 0
    batch = 1 << 16

    for start in range(0, total, batch):
        end = min(start + batch, total)
        masks = np.arange(start, end, dtype=np.int64)
        cur_w = np.zeros(end - start, dtype=np.int64)
        cur_v = np.zeros(end - start, dtype=object)
        for i in range(n):
            bit = (masks >> i) & 1
            cur_w += bit * weights[i]
            cur_v += bit * values[i]
        feasible = cur_w <= capacity
        for j in range(end - start):
            if feasible[j]:
                v = int(cur_v[j])
                if v > best:
                    best = v
                    best_count = 1
                elif v == best:
                    best_count += 1
    return best, best_count
