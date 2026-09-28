"""求和内核分发入口。

三种方法共享同一组特殊值规则（见 policy.py），差别只在有限输入上的舍入行为。
"""

from __future__ import annotations

from enum import Enum
from typing import Iterable

from app.kernels.compensated import compensated_sum_chunked
from app.kernels.naive import naive_sum_chunked
from app.kernels.pairwise import pairwise_sum_chunked
from app.kernels.policy import InputProfile, apply_special_rules, normalize_zero, scan_input


class Method(str, Enum):
    NAIVE = "naive"
    PAIRWISE = "pairwise"
    COMPENSATED = "compensated"


ALL_METHODS: tuple[Method, ...] = (Method.NAIVE, Method.PAIRWISE, Method.COMPENSATED)


def run_method(
    method: Method,
    values: Iterable[float],
    chunk_size: int,
    pairwise_block: int = 8,
) -> float:
    """对**已物化**的有限浮点序列执行指定求和方法。

    特殊值（NaN/Inf/带符号零）请走 ``sum_with_policy``，本函数假定输入已扫描。
    """
    data = list(values)
    if method is Method.NAIVE:
        return naive_sum_chunked(data, chunk_size)
    if method is Method.PAIRWISE:
        return pairwise_sum_chunked(data, chunk_size, block=pairwise_block)
    if method is Method.COMPENSATED:
        return compensated_sum_chunked(data, chunk_size)
    raise ValueError(f"未知方法: {method!r}")


def sum_with_policy(
    method: Method,
    values: Iterable[float],
    chunk_size: int,
    pairwise_block: int = 8,
) -> tuple[float, InputProfile]:
    """扫描输入（可能抛出 SummationRejected），应用固定特殊值规则后求和。"""
    data = [float(v) for v in values]
    profile = scan_input(data)
    special = apply_special_rules(profile)
    if special is not None:
        return special, profile
    result = run_method(method, data, chunk_size, pairwise_block)
    return normalize_zero(result, profile), profile
