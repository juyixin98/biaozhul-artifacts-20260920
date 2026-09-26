"""Unit tests for the KD-tree against a brute-force reference."""

import numpy as np
import pytest

from icp2d.kdtree import KDTree2D, brute_force_nearest


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_kdtree_matches_brute_force(seed):
    rng = np.random.default_rng(seed)
    target = rng.uniform(-5.0, 5.0, size=(60, 2))
    query = rng.uniform(-5.0, 5.0, size=(25, 2))
    tree = KDTree2D(target)
    tree_dists, tree_indices = tree.query(query)
    bf_dists, bf_indices = brute_force_nearest(query, target)
    np.testing.assert_allclose(tree_dists, bf_dists, atol=1e-12)
    np.testing.assert_array_equal(tree_indices, bf_indices)


def test_kdtree_exact_hit_returns_zero_distance():
    target = np.array([[0.0, 0.0], [1.0, 1.0], [2.0, 2.0]])
    dists, indices = KDTree2D(target).query(np.array([[1.0, 1.0]]))
    assert dists[0] == 0.0
    assert indices[0] == 1


def test_kdtree_rejects_bad_shapes():
    with pytest.raises(ValueError):
        KDTree2D(np.zeros((3, 3)))
    with pytest.raises(ValueError):
        KDTree2D(np.zeros((0, 2)))
    tree = KDTree2D(np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]]))
    with pytest.raises(ValueError):
        tree.query(np.zeros((2, 3)))
