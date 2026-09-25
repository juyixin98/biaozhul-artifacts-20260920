"""Hard-constraint tests: group isolation, conservation, disjointness."""
import numpy as np

from grouped_splitter import split_dataset


def _assert_partition(result, n):
    """Conservation: every index appears exactly once across all splits."""
    seen = []
    for name in result.split_names:
        seen.extend(result.assignments[name])
    assert len(seen) == n, "sample count not conserved (duplication/loss)"
    assert sorted(seen) == list(range(n)), "assignment is not a partition of 0..n-1"


def _assert_group_isolation(groups, result):
    gid_to_split = {}
    for name in result.split_names:
        for idx in result.assignments[name]:
            gid = groups[idx]
            if gid in gid_to_split:
                assert gid_to_split[gid] == name, (
                    f"group {gid!r} leaks across {gid_to_split[gid]!r} and {name!r}"
                )
            else:
                gid_to_split[gid] = name
    assert set(gid_to_split) == set(groups)
    return gid_to_split


def test_group_isolation_on_synthetic_default(default_dataset, default_result):
    groups = list(default_dataset.group_ids)
    _assert_partition(default_result, default_dataset.n_samples)
    table = _assert_group_isolation(groups, default_result)
    # group_assignments mapping must agree with the index-level assignment.
    assert table == default_result.group_assignments


def test_group_isolation_with_random_row_blocks(rng):
    """Construct groups as contiguous random-size blocks; assert atomicity."""
    n = 1000
    sizes = []
    while sum(sizes) < n:
        sizes.append(int(rng.integers(1, 40)))
    sizes[-1] -= sum(sizes) - n
    groups = np.repeat(np.arange(len(sizes)), sizes)
    labels = rng.integers(0, 4, size=n)
    result = split_dataset(list(groups), list(labels), seed=7)

    _assert_partition(result, n)
    _assert_group_isolation(list(groups), result)
    # Whole-group sizes must be preserved inside each split.
    for gid, name in result.group_assignments.items():
        assert result.split_sizes[name] >= sizes[int(gid)]


def test_splits_pairwise_disjoint(default_result):
    sets = [set(default_result.assignments[s]) for s in default_result.split_names]
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            assert not (sets[i] & sets[j])


def test_split_sizes_match_assignment_lengths(default_result):
    for name in default_result.split_names:
        assert default_result.split_sizes[name] == len(
            default_result.assignments[name]
        )
    assert sum(default_result.split_sizes.values()) == default_result.n_samples


def test_single_group_goes_entirely_to_one_split():
    groups = ["a"] * 50
    labels = [0] * 50
    result = split_dataset(groups, labels, ratios=(0.6, 0.2, 0.2), seed=1)
    non_empty = [s for s in result.split_names if result.split_sizes[s] > 0]
    assert non_empty == ["train"]
    assert result.split_sizes["train"] == 50
    assert not result.diagnostics.stratified_within_tolerance
    assert result.diagnostics.reasons  # structural reason returned
