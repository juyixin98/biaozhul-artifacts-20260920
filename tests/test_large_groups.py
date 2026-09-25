"""Large-group scenarios: atomic groups whose size dominates a split."""
from grouped_splitter import split_dataset


def test_one_giant_group_two_splits_reports_size_deviation():
    # One group is 60% of the data; a 70/30 split cannot match both targets.
    groups = ["giant"] * 600 + [f"s{i}" for i in range(40)]
    labels = [0] * 600 + [1] * 40
    result = split_dataset(groups, labels, ratios=(0.7, 0.3), seed=0, tolerance=0.05)

    # Hard constraint still holds.
    giant_split = result.group_assignments["giant"]
    assert result.split_sizes[giant_split] >= 600
    assert sum(result.split_sizes.values()) == 640

    # The 30% split can only receive at most 40 samples -> deviation flagged
    # with a reason naming the oversized atomic group.
    assert not result.diagnostics.counts_within_tolerance
    assert "giant" in result.diagnostics.oversized_groups
    assert any("atomic" in r and "giant" in r for r in result.diagnostics.reasons)


def test_large_group_in_three_way_split(default_dataset, default_result):
    # Synthetic fixture: a single ~25% group, default 70/15/15 ratios.
    table = default_dataset.group_size_table()
    largest_gid = max(table, key=table.get)
    largest_frac = table[largest_gid] / default_dataset.n_samples
    assert largest_frac > 0.20

    assigned = default_result.group_assignments[largest_gid]
    # The split carrying the giant group has it whole.
    assert assigned in default_result.split_names
    assert default_result.split_sizes[assigned] >= table[largest_gid]


def test_many_equal_groups_hit_targets_closely():
    # 100 identical-size groups over 2 classes: allocation can be very close.
    groups, labels = [], []
    for i in range(100):
        label = i % 2
        groups.extend([f"g{i:03d}"] * 10)
        labels.extend([label] * 10)
    result = split_dataset(groups, labels, ratios=(0.8, 0.1, 0.1), seed=4)
    for name, ratio in zip(result.split_names, (0.8, 0.1, 0.1)):
        assert abs(result.split_sizes[name] / 1000 - ratio) <= 0.02
    assert result.diagnostics.stratified_within_tolerance


def test_two_groups_three_splits_one_split_empty():
    groups = ["a"] * 50 + ["b"] * 50
    labels = [0] * 50 + [1] * 50
    result = split_dataset(groups, labels, ratios=(0.6, 0.2, 0.2), seed=2)
    assert sum(result.split_sizes.values()) == 100
    assert any("empty" in r for r in result.diagnostics.reasons)
