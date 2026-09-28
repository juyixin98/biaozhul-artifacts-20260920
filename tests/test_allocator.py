"""Tests for the half-open global root allocator shared by numeric witnesses."""

from __future__ import annotations

from fractions import Fraction as F

from root_isolator.evidence.allocator import assign_half_open, cluster_roots


def test_strict_interior_assignment():
    # intervals (0,2], (2,4]
    alloc = assign_half_open([F(0), F(2)], [F(2), F(4)], [F(1), F(3)], F(1, 1000))
    assert alloc.clusters_per_interval == ((0,), (1,))
    assert alloc.unassigned == ()


def test_root_at_shared_endpoint_goes_to_left_interval():
    # A numerical root exactly at the shared point 2 belongs to (0,2].
    alloc = assign_half_open([F(0), F(2)], [F(2), F(4)], [F(2)], F(1, 1000))
    assert alloc.clusters_per_interval == ((0,), ())


def test_root_just_above_shared_endpoint_goes_to_right_interval():
    root = F(2) + F(1, 10 ** 6)
    alloc = assign_half_open([F(0), F(2)], [F(2), F(4)], [root], F(1, 1000))
    assert alloc.clusters_per_interval == ((), (0,))


def test_root_in_gap_is_unassigned():
    # Intervals (0,1] and (3,4]; a root at 2 is in a genuine gap.
    alloc = assign_half_open([F(0), F(3)], [F(1), F(4)], [F(2)], F(1, 1000))
    assert alloc.clusters_per_interval == ((), ())
    assert alloc.unassigned == (0,)


def test_endpoint_noise_snaps_to_owner():
    # Root at 2, computed slightly to the left by numerical noise; the right
    # interval (2,4] must NOT steal it from (0,2].
    noisy = F(2) - F(1, 10 ** 9)
    alloc = assign_half_open([F(0), F(2)], [F(2), F(4)], [noisy], F(1, 10 ** 6))
    assert alloc.clusters_per_interval == ((0,), ())


def test_clustering_merges_split_eigenvalues():
    roots = [F(1), F(1) + F(1, 10 ** 12), F(3)]
    clusters = cluster_roots(roots, F(1, 10 ** 6))
    assert len(clusters) == 2
    assert len(clusters[0].members) == 2
    assert clusters[1].members == (F(3),)


def test_clustering_keeps_genuinely_separate_roots():
    roots = [F(1), F(1) + F(1, 10 ** 3), F(3)]
    clusters = cluster_roots(roots, F(1, 10 ** 6))
    assert len(clusters) == 3


def test_no_double_counting_across_three_intervals():
    left = [F(0), F(2), F(4)]
    right = [F(2), F(4), F(6)]
    # Roots at the shared endpoint 2 and in the interior of the last interval.
    centers = [F(2), F(5)]
    alloc = assign_half_open(left, right, centers, F(1, 1000))
    # Cluster 0 (root exactly at shared point 2) is owned by interval 0, and
    # interval 1 must not also claim it; cluster 1 belongs to interval 2.
    assert alloc.owner_of_cluster == (0, 2)
    assert alloc.clusters_per_interval == ((0,), (), (1,))
    assert alloc.unassigned == ()
