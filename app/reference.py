"""mpmath 高精度参考和。

输入是 float64，每个值都是二进制有理数，mpmath 在足够位宽下对其求和
可视为"精确参考"（默认 50 位十进制，约 166 位二进制，远超 float64 的 53 位）。
本模块只负责给参考值，误差解读见 error_analysis.py。
"""

from __future__ import annotations

from typing import Sequence

import mpmath
from mpmath import mpf

DEFAULT_DPS = 50


def high_precision_sum(values: Sequence[float], dps: int = DEFAULT_DPS) -> mpf:
    with mpmath.workdps(dps):
        total = mpf(0)
        for v in values:
            total += mpf(v)
        return +total


def high_precision_sum_abs(values: Sequence[float], dps: int = DEFAULT_DPS) -> mpf:
    with mpmath.workdps(dps):
        total = mpf(0)
        for v in values:
            total += abs(mpf(v))
        return +total
