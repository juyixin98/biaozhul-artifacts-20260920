"""Acceptance tests for the group-constrained stratified splitter."""

from __future__ import annotations

import numpy as np
import pytest

from group_split import split_groups
from group_split.splitter import (
    REASON_LARGE_GROUP,
    REASON_RARE_CLASS,
)
from group_split import synthetic

RATIOS = {"train": 0.7, "val": 0.15, "test": 0.15}
SEED = 42


def assert_group_isolation(result, groups) -> None:
    """No group id may appear in more than one split."""
    groups = np.asarray(groups)
    seen: dict[object, str] = {}
    for split_name, idx in result.split_indices.items():
        for g in groups[idx]:
            g = g.item() if isinstance(g, np.generic) else g
            assert seen.get(g, split_name) == split_name, (
                f"group {g!r} spans splits {seen[g]!r} and {split_name!r}"
            )
            seen[g] = split_name
    # Assignment map agrees with the indices.
    assert seen == result.assignment


def assert_total_conservation(result, n_samples: int) -> None:
    """Every sample is assigned to exactly one split."""
    all_idx = np.concatenate([idx for idx in result.split_indices.values()])
    assert all_idx.size == n_samples
    assert np.array_equal(np.sort(all_idx), np.arange(n_samples))


# --- hard invariants ---------------------------------------------------------


def test_group_isolation_and_conservation_balanced():
    groups, labels = synthetic.make_balanced(seed=SEED)
    result = split_groups(groups, labels, RATIOS, seed=SEED)
    assert_group_isolation(result, groups)
    assert_total_conservation(result, len(groups))


def test_group_isolation_and_conservation_large_group():
    groups, labels = synthetic.make_with_large_group(seed=SEED)
    result = split_groups(groups, labels, RATIOS, seed=SEED)
    assert_group_isolation(result, groups)
    assert_total_conservation(result, len(groups))


def test_group_isolation_and_conservation_rare_class():
    groups, labels = synthetic.make_with_rare_class(seed=SEED)
    result = split_groups(groups, labels, RATIOS, seed=SEED)
    assert_group_isolation(result, groups)
    assert_total_conservation(result, len(groups))


# --- reproducibility ---------------------------------------------------------


def test_fixed_seed_is_stable():
    groups, labels = synthetic.make_balanced(seed=SEED)
    first = split_groups(groups, labels, RATIOS, seed=SEED)
    second = split_groups(groups, labels, RATIOS, seed=SEED)
    assert first.assignment == second.assignment
    for name in RATIOS:
        assert np.array_equal(first.split_indices[name], second.split_indices[name])


def test_different_seed_gives_valid_split():
    groups, labels = synthetic.make_balanced(seed=SEED)
    other = split_groups(groups, labels, RATIOS, seed=SEED + 1)
    assert_group_isolation(other, groups)
    assert_total_conservation(other, len(groups))
    # A different seed should almost surely reshuffle some group.
    base = split_groups(groups, labels, RATIOS, seed=SEED)
    assert base.assignment != other.assignment


def test_input_reordering_is_invariant():
    groups, labels = synthetic.make_balanced(seed=SEED)
    perm = np.random.default_rng(7).permutation(len(groups))
    base = split_groups(groups, labels, RATIOS, seed=SEED)
    shuffled = split_groups(groups[perm], labels[perm], RATIOS, seed=SEED)
    assert base.assignment == shuffled.assignment


# --- stratification quality --------------------------------------------------


def test_stratification_within_tolerance_on_balanced_data():
    groups, labels = synthetic.make_balanced(seed=SEED)
    result = split_groups(groups, labels, RATIOS, seed=SEED)
    assert result.within_tolerance, result.report["reasons"]
    assert result.report["max_deviation"] <= result.report["tolerance"]


# --- conflict cases: deviation + reasons ------------------------------------


def test_large_group_forces_deviation_and_reason():
    groups, labels = synthetic.make_with_large_group(seed=SEED)
    result = split_groups(groups, labels, RATIOS, seed=SEED)
    assert not result.within_tolerance
    codes = {r["code"] for r in result.report["reasons"]}
    assert REASON_LARGE_GROUP in codes


def test_rare_class_forces_deviation_and_reason():
    groups, labels = synthetic.make_with_rare_class(seed=SEED)
    result = split_groups(groups, labels, RATIOS, seed=SEED)
    assert not result.within_tolerance
    codes = {r["code"] for r in result.report["reasons"]}
    assert REASON_RARE_CLASS in codes
    # At least one split must contain zero rare-class samples.
    assert any(
        s["class_counts"].get("rare", 0) == 0
        for s in result.report["splits"].values()
    )


# --- input validation --------------------------------------------------------


def test_length_mismatch_rejected():
    with pytest.raises(ValueError, match="length mismatch"):
        split_groups([1, 2, 3], [0, 1], RATIOS)


def test_empty_dataset_rejected():
    with pytest.raises(ValueError, match="empty"):
        split_groups([], [], RATIOS)


def test_ratios_must_sum_to_one():
    with pytest.raises(ValueError, match="sum to 1.0"):
        split_groups([1, 2], [0, 1], {"train": 0.5, "test": 0.6})


def test_ratios_must_be_positive():
    with pytest.raises(ValueError, match="positive"):
        split_groups([1, 2], [0, 1], {"train": 1.0, "test": 0.0})


def test_tolerance_must_be_positive():
    with pytest.raises(ValueError, match="tolerance"):
        split_groups([1, 2], [0, 1], RATIOS, tolerance=0.0)
