"""Core math tests: sign equivalence, 180° boundary, outlier robustness,
orthonormality of the output matrix, and small-angle analytic reference."""

import numpy as np
import pytest

from app.rotation import (
    average_rotations,
    geodesic_angle,
    quat_to_matrix,
    rotvec_to_quat,
)


def axis_angle(axis, angle_rad):
    axis = np.asarray(axis, dtype=float)
    axis /= np.linalg.norm(axis)
    return rotvec_to_quat(axis * angle_rad)


class TestSignEquivalence:
    def test_flipped_inputs_same_average(self):
        rng = np.random.default_rng(42)
        base = axis_angle([1, 2, 3], 0.7)
        quats = np.array([
            quat_mul_noise(base, rng, 0.05) for _ in range(8)
        ])
        flipped = quats.copy()
        flipped[::2] *= -1.0  # q and -q are the same rotation

        r1 = average_rotations(quats)
        r2 = average_rotations(flipped)

        assert r1.converged and r2.converged
        assert geodesic_angle(r1.quaternion, r2.quaternion) < 1e-10

    def test_single_quaternion_and_its_negative(self):
        q = axis_angle([0, 0, 1], 1.0)
        r1 = average_rotations([q])
        r2 = average_rotations([-q])
        assert geodesic_angle(r1.quaternion, q) < 1e-12
        assert geodesic_angle(r2.quaternion, q) < 1e-12


class TestBoundary180:
    def test_average_across_180_degree_boundary(self):
        # +179° and -179° about z are 2° apart on SO(3) but straddle ±π.
        q_plus = axis_angle([0, 0, 1], np.deg2rad(179.0))
        q_minus = axis_angle([0, 0, 1], np.deg2rad(-179.0))
        result = average_rotations([q_plus, q_minus])

        expected = axis_angle([0, 0, 1], np.pi)  # 180° about z
        assert result.converged
        assert geodesic_angle(result.quaternion, expected) < 1e-9

    def test_cluster_around_180(self):
        rng = np.random.default_rng(7)
        center = axis_angle([0, 1, 0], np.pi - 0.01)
        quats = [quat_mul_noise(center, rng, 0.02) for _ in range(10)]
        result = average_rotations(quats)
        assert result.converged
        assert geodesic_angle(result.quaternion, center) < 0.02


class TestOutlier:
    def test_single_outlier_is_downweighted(self):
        rng = np.random.default_rng(1)
        inliers = [quat_mul_noise(np.array([1.0, 0, 0, 0]), rng, 0.02)
                   for _ in range(6)]
        outlier = axis_angle([1, 0, 0], np.pi / 2)  # 90° away
        result = average_rotations(inliers + [outlier])

        identity = np.array([1.0, 0.0, 0.0, 0.0])
        assert result.converged
        # Robust mean stays close to the inlier cluster.
        assert geodesic_angle(result.quaternion, identity) < np.deg2rad(5.0)
        # The outlier is flagged and gets the smallest robust weight.
        assert result.outlier_indices == [6]
        assert result.robust_weights[6] == pytest.approx(
            min(result.robust_weights))

    def test_unweighted_mean_is_pulled_by_outlier(self):
        # Sanity check that the robustness actually matters: with a huge
        # huber_delta the scheme reduces to (near-)L2 and the mean moves.
        inliers = [np.array([1.0, 0, 0, 0])] * 6
        outlier = axis_angle([1, 0, 0], np.pi / 2)
        robust = average_rotations(inliers + [outlier])
        naive = average_rotations(inliers + [outlier], huber_delta=1e9)
        identity = np.array([1.0, 0.0, 0.0, 0.0])
        assert geodesic_angle(robust.quaternion, identity) < \
            geodesic_angle(naive.quaternion, identity)


class TestRotationMatrix:
    def test_output_matrix_is_proper_rotation(self):
        rng = np.random.default_rng(3)
        quats = [rotvec_to_quat(rng.normal(scale=0.5, size=3)) for _ in range(12)]
        result = average_rotations(quats)
        r = result.rotation_matrix
        assert np.allclose(r @ r.T, np.eye(3), atol=1e-12)
        assert np.linalg.det(r) == pytest.approx(1.0, abs=1e-12)
        # Quaternion is unit norm.
        assert np.linalg.norm(result.quaternion) == pytest.approx(1.0, abs=1e-12)


class TestAnalyticReference:
    def test_small_angles_about_same_axis(self):
        # For small rotations about a fixed axis, the geodesic average equals
        # the weighted average of the angles (linearised exactly on SO(2)).
        angles = np.array([0.01, 0.02, 0.03])
        weights = np.array([1.0, 2.0, 3.0])
        quats = [axis_angle([0, 0, 1], a) for a in angles]
        result = average_rotations(quats, weights)

        expected_angle = float(np.dot(weights, angles) / weights.sum())
        expected = axis_angle([0, 0, 1], expected_angle)
        assert result.converged
        assert geodesic_angle(result.quaternion, expected) < 1e-12

    def test_two_quaternions_geodesic_midpoint(self):
        # With equal weights the mean of two rotations about the same axis is
        # the midpoint angle — exact on the SO(2) subgroup.
        q1 = axis_angle([1, 1, 0], 0.3)
        q2 = axis_angle([1, 1, 0], 0.5)
        result = average_rotations([q1, q2])
        expected = axis_angle([1, 1, 0], 0.4)
        assert geodesic_angle(result.quaternion, expected) < 1e-12


class TestMultiSolutionHint:
    def test_symmetric_distribution_triggers_hint(self):
        # Identity and 180° about x with equal weight: two equally valid means.
        quats = [np.array([1.0, 0, 0, 0]), np.array([0.0, 1.0, 0, 0])]
        result = average_rotations(quats)
        assert result.multi_solution_hint
        assert any("multiple averages" in m for m in result.messages)

    def test_tight_cluster_no_hint(self):
        rng = np.random.default_rng(9)
        quats = [quat_mul_noise(np.array([1.0, 0, 0, 0]), rng, 0.05)
                 for _ in range(10)]
        result = average_rotations(quats)
        assert not result.multi_solution_hint


class TestValidation:
    def test_zero_norm_quaternion_rejected(self):
        with pytest.raises(ValueError, match="zero-norm"):
            average_rotations([[0.0, 0, 0, 0]])

    def test_weight_length_mismatch_rejected(self):
        with pytest.raises(ValueError, match="weights"):
            average_rotations([[1.0, 0, 0, 0], [1.0, 0, 0, 0]], [1.0])

    def test_negative_weight_rejected(self):
        with pytest.raises(ValueError, match="positive"):
            average_rotations([[1.0, 0, 0, 0]], [-1.0])


def quat_mul_noise(q, rng, sigma):
    """Perturb a quaternion by a small random rotation."""
    noise = rotvec_to_quat(rng.normal(scale=sigma, size=3))
    from app.rotation import quat_multiply
    return quat_multiply(q, noise)
