"""共享工具：用 fractions.Fraction 做独立于被测核心的精确参考。

每个 float64 都是二进制有理数，Fraction(v) 精确无舍入；
Fraction 求和是精确有理数运算，与被测核心的任何浮点路径无关。
"""

from __future__ import annotations

from fractions import Fraction
from typing import Sequence


def exact_sum(values: Sequence[float]) -> Fraction:
    total = Fraction(0)
    for v in values:
        total += Fraction(v)
    return total


def exact_sum_abs(values: Sequence[float]) -> Fraction:
    total = Fraction(0)
    for v in values:
        total += abs(Fraction(v))
    return total
