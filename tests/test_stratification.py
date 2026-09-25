"""Tests for stratification quality, rare classes and deviation reporting."""
import numpy as np

from grouped_splitter import generate_synthetic_dataset, split_dataset


def test_balanced_case_is_within_tolerance():
    """Many small single-class groups -> near-perfect stratification."""
    rng = np.random.default_rng(0)
    groups, labels = [], []
    for gid in range(300):
        label = int(rng.integers(0, 3))
        size = int(rng.integers(2, 6))
        groups.extend([f"g{gid}"] * size)
        labels.extend([label] * size)

    result = split_dataset(groups, labels, seed=42, tolerance=0.05)
    assert result.diagnostics.stratified_within_tolerance
    assert result.diagnostics.counts_within_tolerance
    assert result.diagnostics.max_class_proportion_deviation <= 0.05


def test_rare_class_cannot_appear_in_every_split():
    """A class with one group must be absent from >= 2 of 3 splits."""
    groups = [f"c{i}" for i in range(3)] * 100  # 3 large groups
    groups += ["rare"] * 2                        # 1 tiny rare group
    labels = [0, 1, 2] * 100 + [3] * 2

    result = split_dataset(groups, labels, seed=1, tolerance=0.05)
    rare_counts = {
        s: next(cd.split_counts[s] for cd in result.class_deviations if cd.label == "3")
        for s in result.split_names
    }
    present_in = [s for s, c in rare_counts.items() if c > 0]
    assert len(present_in) == 1
    # The deviation and the structural reason are reported truthfully.
    assert "3" in result.diagnostics.rare_classes
    assert any(
        "Class 3 appears" in r and "unavoidable" in r
        for r in result.diagnostics.reasons
    )
    # Two of the three splits contain 0 rare-class samples vs global 2/302.
    assert result.diagnostics.max_class_proportion_deviation >= 2 / 302 - 1e-12
    assert result.diagnostics.max_class_proportion_deviation > 0


def test_synthetic_dataset_has_rare_and_large_groups(default_dataset):
    table = default_dataset.group_size_table()
    sizes = np.asarray(list(table.values()))
    # One group holds ~25% of all samples.
    assert sizes.max() >= 0.20 * default_dataset.n_samples
    # The rare class (label 0) exists as exactly one tiny group.
    rare_gids = {
        str(g) for g in np.unique(default_dataset.group_ids)
        if (default_dataset.y[default_dataset.group_ids == g] == 0).all()
    }
    assert len(rare_gids) == 1
    assert table[next(iter(rare_gids))] == 3


def test_synthetic_rare_class_deviation_is_flagged(default_result):
    assert "0" in default_result.diagnostics.rare_classes
    reasons = " ".join(default_result.diagnostics.reasons)
    assert "unavoidable" in reasons


def test_two_split_rare_class_can_reach_both():
    """With 2 splits a single-group rare class CAN appear in both? No -- one
    group is still atomic: it lands in exactly one split."""
    groups = ["a"] * 50 + ["b"] * 50 + ["rare"] * 4
    labels = [0] * 50 + [1] * 50 + [2] * 4
    result = split_dataset(groups, labels, ratios=(0.8, 0.2), seed=3)
    rare = next(cd for cd in result.class_deviations if cd.label == "2")
    assert sum(1 for c in rare.split_counts.values() if c > 0) == 1


def test_per_class_proportions_are_recomputed_consistently(default_result):
    for cd in default_result.class_deviations:
        for s in default_result.split_names:
            denom = default_result.split_sizes[s]
            expected = cd.split_counts[s] / denom if denom else 0.0
            assert np.isclose(cd.split_proportions[s], expected)
        assert np.isclose(cd.global_proportion, cd.global_count / default_result.n_samples)
