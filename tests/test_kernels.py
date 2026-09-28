"""内核行为测试：硬编码期望值 + Fraction 独立精确参考。"""

from __future__ import annotations

import random

import pytest

from app.kernels import Method, run_method
from app.kernels.compensated import compensated_sum
from app.kernels.naive import naive_sum
from app.kernels.pairwise import pairwise_sum

from conftest import exact_sum, exact_sum_abs

EPS = 2.0**-52


def test_naive_cancellation_loses_small_term() -> None:
    # 硬编码已知事实：1e16 + 1 在 float64 中舍入回 1e16
    assert naive_sum([1e16, 1.0, -1e16]) == 0.0


def test_pairwise_cancellation_preserves_small_term() -> None:
    # [1, 1, 1e17, -1e17]：ulp(1e17)=16，朴素法中 2 被吞掉得 0；
    # 配对法（块长 2）先算 (1+1) 与 (1e17-1e17)，得真值 2
    data = [1.0, 1.0, 1e17, -1e17]
    assert naive_sum(data) == 0.0
    assert pairwise_sum(data, block=2) == 2.0


def test_compensated_cancellation_preserves_small_term() -> None:
    assert compensated_sum([1e16, 1.0, -1e16]) == 1.0


def test_pairwise_small_exact() -> None:
    assert pairwise_sum([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]) == 45.0


def test_compensated_matches_fraction_exact_on_random_dyadics() -> None:
    rng = random.Random(42)
    values = [rng.uniform(-1e6, 1e6) for _ in range(2000)]
    exact = exact_sum(values)
    got = compensated_sum(values)
    # 补偿法误差应远小于朴素法的一阶界；这里给 8 eps * sum|x| 的宽松上界
    sum_abs = float(exact_sum_abs(values))
    assert abs(float(exact) - got) <= 8 * EPS * sum_abs


def test_pairwise_within_log_bound_against_fraction() -> None:
    import math

    rng = random.Random(7)
    values = [rng.uniform(-1e4, 1e4) for _ in range(5000)]
    exact = exact_sum(values)
    got = pairwise_sum(values)
    sum_abs = float(exact_sum_abs(values))
    bound = math.ceil(math.log2(len(values))) * EPS * sum_abs
    assert abs(float(exact) - got) <= bound


def test_run_method_dispatch() -> None:
    # 分块后朴素法与配对法都丢补偿（得 0），补偿法保留状态（得 1）
    data = [1e16, 1.0, -1e16]
    assert run_method(Method.NAIVE, data, chunk_size=2) == 0.0
    assert run_method(Method.PAIRWISE, data, chunk_size=2) == 0.0
    assert run_method(Method.COMPENSATED, data, chunk_size=2) == 1.0


def test_invalid_chunk_size_rejected() -> None:
    with pytest.raises(ValueError):
        run_method(Method.COMPENSATED, [1.0], chunk_size=0)
