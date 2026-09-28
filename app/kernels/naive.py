"""朴素求和：严格按输入顺序逐项累加。

分块版本先对每块顺序累加得到块和，再按块序把块和逐项累加。
这是"重排敏感"的基准方法：任何求和顺序的改变都可能改变结果。
"""

from __future__ import annotations

from typing import Sequence


def naive_sum(values: Sequence[float]) -> float:
    total = 0.0
    for v in values:
        total += v
    return total


def naive_sum_chunked(values: Sequence[float], chunk_size: int) -> float:
    if chunk_size < 1:
        raise ValueError("chunk_size 必须 >= 1")
    total = 0.0
    for start in range(0, len(values), chunk_size):
        total += naive_sum(values[start : start + chunk_size])
    return total
