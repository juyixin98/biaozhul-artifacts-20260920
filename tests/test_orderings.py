"""Tests showing how rearrangement affects each method.

Locked concrete results, seeded shuffles, and order-of-error comparisons.
"""
from __future__ import annotations

import numpy as np

from app.core import kernels
from app.services import comparison, synthetic


def _result(values: np.ndarray, ordering: str, method: str, block_size: int = 4096, seed: int = 3):
    payload = comparison.run_comparison(values, block_size, [ordering], shuffle_seed=seed)
    entry = payload["orderings"][ordering]
    if method in comparison.MONOLITHIC_METHODS:
        return entry["methods"][method]["result"]
    return entry["chunked"][method]["result"]


def test_big_cancel_ordering_changes_naive_but_not_kahan():
    values = synthetic.big_cancel(10)
    # Original layout: large positive first, ones absorbed, large negative cancels.
    assert _result(values, "original", "naive") == 0.0
    # Smallest-first keeps the ones resolvable.
    assert _result(values, "abs_ascending", "naive") == 10.0
    # Kahan is rearrangement-invariant on this input (and everywhere to O(u)).
    for ordering in ("original", "reversed", "blocks_reversed", "abs_ascending", "abs_descending"):
        assert _result(values, ordering, "kahan") == 10.0


def test_seeded_shuffle_is_deterministic():
    values = synthetic.big_cancel(10)
    first = _result(values, "shuffle", "naive", seed=3)
    second = _result(values, "shuffle", "naive", seed=3)
    third = _result(values, "shuffle", "naive", seed=4)
    assert first == second == 7.0
    assert third != first


def test_random_mixed_scale_naive_changes_under_shuffle_kahan_does_not():
    rng = np.random.default_rng(0)
    values = rng.uniform(-1, 1, 5000) * 10.0 ** rng.integers(-12, 12, 5000)
    payload = comparison.run_comparison(values, 512, ["original", "shuffle"], shuffle_seed=7)
    original = payload["orderings"]["original"]["methods"]
    shuffled = payload["orderings"]["shuffle"]["methods"]
    assert original["naive"]["result"] != shuffled["naive"]["result"]
    assert original["kahan"]["result"] == shuffled["kahan"]["result"]


def test_abs_ascending_beats_given_order_for_naive_alternating_series():
    values = synthetic.alternating(200_000)
    payload = comparison.run_comparison(
        values, 4096, ["original", "abs_ascending", "abs_descending"]
    )
    err = {
        name: payload["orderings"][name]["methods"]["naive"]["abs_error"]
        for name in ("original", "abs_ascending", "abs_descending")
    }
    # Ascending magnitude accumulation loses far fewer bits than the
    # large-first order: locked concrete measured values.
    assert err["abs_ascending"] == 1.1102230246251565e-16
    assert err["original"] == 3.7969627442180354e-14
    assert err["abs_descending"] == err["original"]
    assert err["original"] >= 300 * err["abs_ascending"]


def test_block_size_does_not_change_streaming_kahan_but_changes_pairwise_tree():
    values = synthetic.big_cancel(100_000)
    p4096 = _result(values, "original", "blocked_pairwise", block_size=4096)
    p128 = _result(values, "original", "blocked_pairwise", block_size=128)
    k4096 = _result(values, "original", "kahan_streaming", block_size=4096)
    k128 = _result(values, "original", "kahan_streaming", block_size=128)
    assert k4096 == k128 == 100_000.0
    # Narrower leaves group the small terms with fewer large-ulp neighbours,
    # so different block widths genuinely restructure the reduction tree and
    # change how many ones survive (locked concrete values).
    assert p4096 == 95_904.0
    assert p128 == 99_872.0
    assert p128 != p4096


def test_every_ordering_reports_true_invariants():
    values = synthetic.mixed_scales(3000, seed=5)
    payload = comparison.run_comparison(
        values, 37, ["original", "reversed", "blocks_reversed", "abs_ascending", "shuffle"],
        shuffle_seed=1,
    )
    for ordering, entry in payload["orderings"].items():
        invariants = entry["invariants"]
        assert invariants["blocked_naive_equals_naive"] is True, ordering
        assert invariants["kahan_streaming_equals_monolithic"] is True, ordering
        assert invariants["kahan_merged_meets_compensated_bound"] is True, ordering
