"""配对（级联）求和：块内递归两两合并，块和再按同样方式两两合并。

误差界为 O(eps * log2 n) * sum|x|，优于朴素法的 O(eps * n)。
基线块（<= block 个元素）用 numpy 求和（numpy 内部同样是配对策略）。
"""

from __future__ import annotations

from typing import Sequence

import numpy as np


def pairwise_sum(values: Sequence[float], block: int = 8) -> float:
    n = len(values)
    if n == 0:
        return 0.0
    if n <= block:
        return float(np.sum(np.asarray(values, dtype=np.float64)))
    mid = n // 2
    return pairwise_sum(values[:mid], block) + pairwise_sum(values[mid:], block)


def _pairwise_merge_totals(totals: list[float], block: int) -> float:
    return pairwise_sum(totals, block)


def pairwise_sum_chunked(values: Sequence[float], chunk_size: int, block: int = 8) -> float:
    if chunk_size < 1:
        raise ValueError("chunk_size 必须 >= 1")
    totals = [
        pairwise_sum(values[start : start + chunk_size], block)
        for start in range(0, len(values), chunk_size)
    ]
    return _pairwise_merge_totals(totals, block)
