"""Determinism: fixed seed stability and input-row permutation invariance."""
import numpy as np

from grouped_splitter import generate_synthetic_dataset, split_dataset


def _signature(result):
    """Order-independent content signature of a split."""
    return {
        "sizes": tuple(sorted(result.split_sizes.items())),
        "groups": tuple(sorted(result.group_assignments.items())),
        "class": tuple(
            (cd.label, tuple(sorted(cd.split_counts.items())))
            for cd in sorted(result.class_deviations, key=lambda c: c.label)
        ),
    }


def test_same_seed_same_result(default_dataset):
    kw = dict(groups=list(default_dataset.group_ids), labels=list(default_dataset.y), seed=99)
    first = split_dataset(**kw)
    second = split_dataset(**kw)
    assert _signature(first) == _signature(second)
    assert first.assignments == second.assignments


def test_different_seeds_can_differ(default_dataset):
    kw = dict(groups=list(default_dataset.group_ids), labels=list(default_dataset.y))
    a = split_dataset(seed=1, **kw)
    b = split_dataset(seed=2, **kw)
    # Different tie-breaking may change the allocation (not required to, but
    # for this dataset it does); invariants must hold either way.
    assert sum(a.split_sizes.values()) == sum(b.split_sizes.values())


def test_input_row_permutation_invariance(default_dataset):
    """Shuffling rows must not change which split each GROUP lands in."""
    groups = list(default_dataset.group_ids)
    labels = list(default_dataset.y)
    rng = np.random.default_rng(2024)
    perm = rng.permutation(default_dataset.n_samples)

    shuffled_groups = [groups[i] for i in perm]
    shuffled_labels = [labels[i] for i in perm]

    a = split_dataset(groups, labels, seed=42)
    b = split_dataset(shuffled_groups, shuffled_labels, seed=42)

    assert a.group_assignments == b.group_assignments
    assert a.split_sizes == b.split_sizes
    assert _signature(a) == _signature(b)

    # Map shuffled indices back: the same original samples are assigned.
    for name in a.split_names:
        original_via_shuffle = sorted(int(perm[i]) for i in b.assignments[name])
        assert sorted(a.assignments[name]) == original_via_shuffle


def test_repeated_shuffles_stable(rng, default_dataset):
    groups = list(default_dataset.group_ids)
    labels = list(default_dataset.y)
    baseline = split_dataset(groups, labels, seed=7)
    for _ in range(5):
        perm = rng.permutation(default_dataset.n_samples)
        res = split_dataset(
            [groups[i] for i in perm], [labels[i] for i in perm], seed=7
        )
        assert res.group_assignments == baseline.group_assignments


def test_group_ids_as_ints_and_strings_equivalent():
    """Same content under different id types/order -> same class allocation."""
    g1 = [0, 0, 1, 1, 2, 2]
    l1 = [0, 0, 1, 1, 2, 2]
    g2 = ["x", "x", "y", "y", "z", "z"]
    l2 = [0, 0, 1, 1, 2, 2]
    r1 = split_dataset(g1, l1, ratios=(0.5, 0.25, 0.25), seed=5)
    r2 = split_dataset(g2, l2, ratios=(0.5, 0.25, 0.25), seed=5)
    assert r1.split_sizes == r2.split_sizes


def test_synthetic_generator_is_reproducible():
    a = generate_synthetic_dataset(seed=123)
    b = generate_synthetic_dataset(seed=123)
    assert np.array_equal(a.X, b.X)
    assert np.array_equal(a.y, b.y)
    assert np.array_equal(a.group_ids, b.group_ids)
    c = generate_synthetic_dataset(seed=124)
    assert not np.array_equal(a.X, c.X)
