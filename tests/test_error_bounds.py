"""误差界核验：实测误差（对 mpmath 高精度参考）必须落在理论界内，
且条件数能解释大数相消输入为何困难。"""

from __future__ import annotations

import math

from app.error_analysis import EPS, build_error_report, condition_number
from app.inputgen import gen_cancellation, gen_random_spread, gen_small_accumulation
from app.kernels import Method, run_method


def _check(method: Method, data: list[float], chunk_size: int = 256) -> None:
    result = run_method(method, data, chunk_size)
    report = build_error_report(method, result, data)
    assert report.within_bound, (
        f"{method.value} 超出理论界: abs_error={report.abs_error} bound={report.bound}"
    )


def test_naive_within_bound_on_random_spread() -> None:
    data = list(gen_random_spread(4000, seed=1))
    _check(Method.NAIVE, data)


def test_pairwise_within_bound_on_random_spread() -> None:
    data = list(gen_random_spread(4000, seed=2))
    _check(Method.PAIRWISE, data)


def test_compensated_within_bound_on_random_spread() -> None:
    data = list(gen_random_spread(4000, seed=3))
    _check(Method.COMPENSATED, data)


def test_compensated_far_tighter_than_naive_on_cancellation() -> None:
    data = list(gen_cancellation(300, magnitude=1e16, small=1.0))
    naive = run_method(Method.NAIVE, data, 256)
    comp = run_method(Method.COMPENSATED, data, 256)
    rep_n = build_error_report(Method.NAIVE, naive, data)
    rep_c = build_error_report(Method.COMPENSATED, comp, data)
    # 真值 300；朴素法在此输入下误差应显著大于补偿法
    assert rep_n.abs_error > 100 * max(rep_c.abs_error, 1e-12)
    assert rep_c.abs_error == 0.0  # 该模式补偿法精确


def test_small_accumulation_compensated_beats_naive() -> None:
    data = list(gen_small_accumulation(100_000, 0.1))
    naive = run_method(Method.NAIVE, data, 10_000)
    comp = run_method(Method.COMPENSATED, data, 10_000)
    rep_n = build_error_report(Method.NAIVE, naive, data)
    rep_c = build_error_report(Method.COMPENSATED, comp, data)
    assert rep_c.abs_error < rep_n.abs_error
    assert rep_n.within_bound and rep_c.within_bound


def test_condition_number_explains_cancellation_difficulty() -> None:
    hard = list(gen_cancellation(100, magnitude=1e16, small=1.0))
    easy = list(gen_small_accumulation(100, 1.0))
    kappa_hard = condition_number(hard)
    kappa_easy = condition_number(easy)
    assert kappa_hard > 1e12
    assert kappa_easy < 10.0
    assert math.isfinite(kappa_hard)


def test_exact_zero_sum_has_infinite_condition_number() -> None:
    assert condition_number([1.0, -1.0, 2.0, -2.0]) == math.inf


def test_eps_matches_float64() -> None:
    assert EPS == 2.0**-52
