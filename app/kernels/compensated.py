"""补偿求和（Kahan-Neumaier），支持**保留补偿状态**的分块合并。

关键点：分块合并不是"各块算出和再相加"。每个块产出的是
``CompensatedState(total, compensation)`` —— 主和加上运行中累计的
舍入补偿；合并两个块状态时用一次 Neumaier 步把 ``b.total`` 并入，
同时把两边的补偿一起携带下去（补偿近似表示被丢弃的低位，线性可加）。
若只把块和相加，等于把每块累计的补偿丢进垃圾桶，在大数相消场景
会立刻露馅（见 tests/test_chunked_merge.py）。

状态对象不可变：所有操作返回新状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class CompensatedState:
    """Neumaier 补偿累加器状态。result = total + compensation。"""

    total: float = 0.0
    compensation: float = 0.0

    def result(self) -> float:
        return self.total + self.compensation


def add_term(state: CompensatedState, x: float) -> CompensatedState:
    """Neumaier（改进 Kahan）单步：对绝对值较大的一方取补偿，避免相消时丢补偿。"""
    t = state.total + x
    if abs(state.total) >= abs(x):
        correction = (state.total - t) + x
    else:
        correction = (x - t) + state.total
    return CompensatedState(total=t, compensation=state.compensation + correction)


def merge(a: CompensatedState, b: CompensatedState) -> CompensatedState:
    """合并两个补偿状态，保留双方补偿。

    把 b.total 当作一个普通项用 Neumaier 步并入 a，再把 a、b 各自累计的
    补偿一并带入新状态的补偿项。
    """
    t = a.total + b.total
    if abs(a.total) >= abs(b.total):
        correction = (a.total - t) + b.total
    else:
        correction = (b.total - t) + a.total
    return CompensatedState(
        total=t,
        compensation=a.compensation + correction + b.compensation,
    )


def state_of_chunk(values: Sequence[float]) -> CompensatedState:
    state = CompensatedState()
    for v in values:
        state = add_term(state, v)
    return state


def compensated_sum(values: Sequence[float]) -> float:
    return state_of_chunk(values).result()


def compensated_sum_chunked(values: Sequence[float], chunk_size: int) -> float:
    """分块补偿求和：逐块求状态，再用 merge 级联合并（补偿状态全程保留）。"""
    if chunk_size < 1:
        raise ValueError("chunk_size 必须 >= 1")
    acc = CompensatedState()
    for start in range(0, len(values), chunk_size):
        acc = merge(acc, state_of_chunk(values[start : start + chunk_size]))
    return acc.result()
