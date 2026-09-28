"""分块合并必须保留补偿状态：构造一个"假合并必错、真合并必对"的用例。

输入：[1e18] + [1.0]*1000 + [-1e18]，真值 1000。
ulp(1e18) = 128，每个块内的小项会被舍入到 128 的倍数；
- 若只把各块的"块和"相加（假合并），补偿被丢弃，结果 = 512；
- 保留补偿状态的合并（CompensatedState + merge）应精确还原 1000。
"""

from __future__ import annotations

from app.kernels.compensated import (
    CompensatedState,
    compensated_sum,
    compensated_sum_chunked,
    merge,
    state_of_chunk,
)
from app.kernels.naive import naive_sum_chunked

from conftest import exact_sum


def _data() -> list[float]:
    return [1e18] + [1.0] * 1000 + [-1e18]


def test_exact_value_is_1000() -> None:
    assert exact_sum(_data()) == 1000


def test_stateful_merge_recovers_exact() -> None:
    assert compensated_sum_chunked(_data(), chunk_size=512) == 1000.0


def test_fake_merge_of_chunk_totals_loses_compensation() -> None:
    # 假合并：各块只取块和（total），相加 -> 512，证明"只加局部结果"不是同一算法
    data = _data()
    chunk1 = state_of_chunk(data[:512])
    chunk2 = state_of_chunk(data[512:])
    fake = chunk1.total + chunk2.total
    assert fake == 512.0
    assert merge(chunk1, chunk2).result() == 1000.0


def test_naive_chunked_also_fails_here() -> None:
    assert naive_sum_chunked(_data(), chunk_size=512) == 512.0


def test_chunk_size_invariance_for_compensated() -> None:
    data = _data()
    expected = compensated_sum(data)
    for chunk_size in (1, 3, 7, 100, 512, 1002, 5000):
        assert compensated_sum_chunked(data, chunk_size) == expected


def test_state_immutable() -> None:
    from app.kernels.compensated import add_term

    s0 = CompensatedState()
    s1 = add_term(s0, 1.5)
    assert s0 == CompensatedState(0.0, 0.0)
    assert s1.total == 1.5
