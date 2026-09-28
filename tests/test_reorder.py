"""重排敏感性：展示何种重排影响何种方法。

固定数据与块大小，只改变块的出现顺序（块内顺序不变）：
- 朴素法：块和按块序累加，块序变则结果变 —— 断言结果确实随块序改变；
- 配对法：块和按块序两两合并，同样可能变，但所有结果都应在配对法理论界内；
- 补偿法：合并保留补偿状态，对块序应高度稳定 —— 断言散布极小且都在界内。
"""

from __future__ import annotations

import numpy as np

from app.error_analysis import build_error_report
from app.inputgen import gen_random_spread, permute_chunks
from app.kernels import Method, run_method

from conftest import exact_sum

N = 4096
CHUNK = 64
TRIALS = 12


def _results_across_permutations(method: Method) -> list[float]:
    data = list(gen_random_spread(N, seed=11, lo_exp=-2.0, hi_exp=10.0))
    n_chunks = N // CHUNK
    rng = np.random.default_rng(99)
    results = []
    for _ in range(TRIALS):
        order = rng.permutation(n_chunks).tolist()
        shuffled = permute_chunks(data, CHUNK, order)
        results.append(run_method(method, shuffled, CHUNK))
    return results


def test_naive_is_reorder_sensitive() -> None:
    results = _results_across_permutations(Method.NAIVE)
    assert len(set(results)) > 1, "朴素法应对块序重排敏感"


def test_compensated_is_reorder_stable() -> None:
    data = list(gen_random_spread(N, seed=11, lo_exp=-2.0, hi_exp=10.0))
    exact = float(exact_sum(data))
    results = _results_across_permutations(Method.COMPENSATED)
    spread = max(results) - min(results)
    # 补偿法在块序重排下的散布应远小于其量级（相对散布 < 1e-12）
    scale = max(abs(exact), 1.0)
    assert spread / scale < 1e-12
    for r in results:
        report = build_error_report(Method.COMPENSATED, r, data)
        assert report.within_bound


def test_pairwise_reorder_results_all_within_bound() -> None:
    data = list(gen_random_spread(N, seed=11, lo_exp=-2.0, hi_exp=10.0))
    results = _results_across_permutations(Method.PAIRWISE)
    for r in results:
        report = build_error_report(Method.PAIRWISE, r, data)
        assert report.within_bound


def test_naive_spread_exceeds_compensated_spread() -> None:
    naive_results = _results_across_permutations(Method.NAIVE)
    comp_results = _results_across_permutations(Method.COMPENSATED)
    naive_spread = max(naive_results) - min(naive_results)
    comp_spread = max(comp_results) - min(comp_results)
    assert naive_spread > 100 * comp_spread
