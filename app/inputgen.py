"""合成输入生成与分块工具。

三类压力输入：
- 大数相消（cancellation）：±M 夹着小项，和的真值远小于项的量级，条件数巨大；
- 小数累积（small_accumulation）：同一小值重复 n 次，考验朴素法的累积舍入；
- 随机宽幅（random_spread）：量级跨多个数量级、随机符号，通用压力输入。

所有生成器都是惰性迭代器，支持"大流式"输入而不必一次物化。
"""

from __future__ import annotations

import itertools
from typing import Iterator, Sequence

import numpy as np


def gen_cancellation(n_pairs: int, magnitude: float = 1e16, small: float = 1.0) -> Iterator[float]:
    """[M, small, -M] 重复 n_pairs 次；真值 = n_pairs * small。"""
    for _ in range(n_pairs):
        yield magnitude
        yield small
        yield -magnitude


def gen_small_accumulation(count: int, value: float = 0.1) -> Iterator[float]:
    """value 重复 count 次；真值 = count * value（value 通常不可精确表示）。"""
    return itertools.repeat(value, count)


def gen_random_spread(n: int, seed: int, lo_exp: float = -3.0, hi_exp: float = 12.0) -> Iterator[float]:
    """n 个随机项：符号随机，量级 10^U[lo_exp, hi_exp]。"""
    rng = np.random.default_rng(seed)
    signs = rng.choice([-1.0, 1.0], size=n)
    exponents = rng.uniform(lo_exp, hi_exp, size=n)
    mantissas = rng.uniform(1.0, 10.0, size=n)
    for s, e, m in zip(signs, exponents, mantissas):
        yield float(s * m * 10.0**e)


def chunk_iter(values: Sequence[float], chunk_size: int) -> Iterator[Sequence[float]]:
    for start in range(0, len(values), chunk_size):
        yield values[start : start + chunk_size]


def permute_chunks(values: Sequence[float], chunk_size: int, order: Sequence[int]) -> list[float]:
    """按给定块序重排（块内顺序不变），用于重排敏感性实验。"""
    chunks = list(chunk_iter(values, chunk_size))
    if sorted(order) != list(range(len(chunks))):
        raise ValueError("order 必须是块下标的一个排列")
    out: list[float] = []
    for idx in order:
        out.extend(chunks[idx])
    return out
