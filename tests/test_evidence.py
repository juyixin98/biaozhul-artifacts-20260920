"""Tests for independent evidence measures and degenerate-subspace logic."""

import numpy as np
import pytest

from sym_eig.evidence.measures import measure
from sym_eig.evidence.subspace import (
    cluster_eigenvalues,
    compare_clusters,
    principal_angles,
    subspace_sines,
)


def test_measure_on_exact_eigendecomposition_is_machine_precision(rng):
    n = 8
    q = np.linalg.qr(rng.standard_normal((n, n)))[0]
    w = rng.standard_normal(n)
    a = (q * w) @ q.T
    evidence = measure(a, w, q)
    assert evidence.residual_fro < 1e-14
    assert evidence.orthogonality_fro < 1e-14
    assert evidence.reconstruction_rel < 1e-14


def test_measure_flags_corrupted_vectors(rng):
    n = 6
    q = np.linalg.qr(rng.standard_normal((n, n)))[0]
    w = np.arange(1.0, n + 1)
    a = (q * w) @ q.T
    bad = q.copy()
    bad[:, 0] = rng.standard_normal(n)  # destroy one eigenvector
    evidence = measure(a, w, bad)
    assert evidence.residual_max_per_eigen > 1e-2
    assert evidence.orthogonality_max > 1e-2


def test_clustering_groups_repeated_and_near_degenerate_values():
    w = np.array([1.0, 1.0 + 1e-13, 4.0, 4.0 + 2e-13, 9.0])
    clusters = cluster_eigenvalues(w, cluster_rtol=1e-8)
    assert [c.multiplicity for c in clusters] == [2, 2, 1]
    assert clusters[0].is_simple is False


def test_clustering_keeps_genuinely_separated_values():
    w = np.array([1.0, 2.0, 3.0])
    clusters = cluster_eigenvalues(w, cluster_rtol=1e-8)
    assert [c.multiplicity for c in clusters] == [1, 1, 1]


def test_degenerate_subspace_rotation_is_recognized_as_match():
    # Same repeated eigenvalues; rotate the triple eigenspace by an arbitrary
    # orthogonal matrix - vectors differ but the subspace is identical.
    rng = np.random.default_rng(11)
    w = np.array([1.0, 1.0, 1.0, 2.0, 5.0])
    q = np.linalg.qr(rng.standard_normal((5, 5)))[0]
    a = (q * w) @ q.T

    ref_w, ref_v = np.linalg.eigh(a)
    result_v = q.copy()
    rotation = np.linalg.qr(rng.standard_normal((3, 3)))[0]
    result_v[:, :3] = ref_v[:, :3] @ rotation

    comparisons = compare_clusters(
        ref_w, result_v, ref_w, ref_v, cluster_rtol=1e-8
    )
    assert [c.multiplicity for c in
           cluster_eigenvalues(ref_w, 1e-8)] == [3, 1, 1]
    triple = next(c for c in comparisons if c.multiplicity == 3)
    assert triple.max_sin_principal_angle < 1e-14
    assert triple.subspace_matches


def test_subspace_distance_is_stable_near_identity():
    # Regression: sqrt(1 - sigma^2) blew up to ~3e-8 for identical subspaces
    # because of round-off in cosines; the stable projection estimate must
    # report true machine precision.
    rng = np.random.default_rng(12)
    q = np.linalg.qr(rng.standard_normal((7, 7)))[0]
    u = q[:, :3]
    w = u @ np.linalg.qr(rng.standard_normal((3, 3)))[0]
    sines = subspace_sines(u, w)
    assert np.max(sines) < 1e-14
    angles = principal_angles(u, w)
    assert np.max(angles) < 1e-13


def test_different_subspaces_are_detected():
    rng = np.random.default_rng(13)
    u = np.eye(5)[:, :2]
    w = np.linalg.qr(rng.standard_normal((5, 5)))[0][:, :2]
    sines = subspace_sines(u, w)
    assert np.max(sines) > 0.5


def test_incompatible_cluster_partition_raises():
    # Our spectrum is a clean triple; the reference has three distinct
    # values. The partitions cannot be paired - that must raise rather than
    # silently compare the wrong spaces.
    w_ours = np.array([1.0, 1.0, 1.0])
    v_ours = np.eye(3)
    w_ref = np.array([1.0, 2.0, 3.0])
    v_ref = np.eye(3)
    with pytest.raises(ValueError, match="cluster structure"):
        compare_clusters(w_ours, v_ours, w_ref, v_ref, cluster_rtol=1e-8)
