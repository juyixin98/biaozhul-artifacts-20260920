"""SE(3) Lie group identity and Jacobian tests."""

import numpy as np
import pytest

from app.se3 import (
    adjoint,
    compose,
    exp_se3,
    exp_so3,
    invert,
    log_se3,
    log_so3,
    make_T,
    quat_to_R,
    skew,
    vee,
)

rng = np.random.default_rng(123)


def random_T(scale=0.4, seed=None):
    r = np.random.default_rng(seed) if seed is not None else rng
    return exp_se3(np.r_[r.standard_normal(3) * 0.2, r.standard_normal(3) * scale])


class TestSO3:
    def test_exp_log_roundtrip(self):
        # |phi| stays inside the principal range (< pi) of the SO(3) log.
        for scale in [1e-8, 1e-3, 0.05, 0.6, 2.5]:
            phi = rng.standard_normal(3)
            phi = phi / np.linalg.norm(phi) * scale if scale > 1e-6 else phi * scale
            assert np.linalg.norm(log_so3(exp_so3(phi)) - phi) < 1e-9

    def test_orthogonal_det_one(self):
        for _ in range(20):
            phi = rng.standard_normal(3) * rng.uniform(0.0, 2.5)
            R = exp_so3(phi)
            assert np.allclose(R @ R.T, np.eye(3), atol=1e-10)
            assert np.isclose(np.linalg.det(R), 1.0)

    def test_skew_vee_inverse(self):
        w = rng.standard_normal(3)
        assert np.allclose(vee(skew(w)), w)

    def test_quat_identity_and_z_rotation(self):
        assert np.allclose(quat_to_R([1, 0, 0, 0]), np.eye(3))
        c, s = np.cos(0.7), np.sin(0.7)
        q = [np.cos(0.35), 0, 0, np.sin(0.35)]
        R = quat_to_R(q)
        Rz = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
        assert np.allclose(R, Rz)


class TestSE3:
    def test_exp_log_roundtrip(self):
        # |phi| stays inside the principal range (< pi) of the SE(3) log;
        # the translation scale is coupled to the rotation scale to exercise
        # the coupled left-Jacobian handling at tiny angles.
        for scale in [1e-9, 1e-4, 0.01, 0.05, 0.6]:
            xi = np.r_[
                rng.standard_normal(3) * (0.1 * max(scale, 1e-6)),
                rng.standard_normal(3) * scale,
            ]
            assert np.linalg.norm(log_se3(exp_se3(xi)) - xi) < 1e-8

    def test_inverse(self):
        T = random_T()
        assert np.allclose(compose(T, invert(T)), np.eye(4), atol=1e-12)
        assert np.allclose(compose(invert(T), T), np.eye(4), atol=1e-12)

    def test_adjoint_identity(self):
        # Exp(Ad(T) x) T = T Exp(x)
        T = random_T()
        x = rng.standard_normal(6) * 1e-4
        lhs = exp_se3(adjoint(T) @ x) @ T
        rhs = T @ exp_se3(x)
        assert np.allclose(lhs, rhs, atol=1e-12)

    def test_adjoint_homomorphism(self):
        A, B = random_T(), random_T()
        assert np.allclose(adjoint(A @ B), adjoint(A) @ adjoint(B), atol=1e-12)

    def test_adjoint_inverse(self):
        T = random_T()
        assert np.allclose(
            adjoint(invert(T)), np.linalg.inv(adjoint(T)), atol=1e-12
        )


class TestChainJacobians:
    """Finite-difference check of the first-order chain Jacobians."""

    @pytest.mark.parametrize("convention", ["right", "left"])
    def test_jacobian_matches_finite_difference(self, convention):
        from app.graph import Edge, Step, step_jacobian

        Xs = [random_T(seed=10 + i) for i in range(4)]
        steps = [
            Step(
                edge=Edge(
                    edge_id=f"e{i}",
                    parent_frame=f"F{i+1}",
                    child_frame=f"F{i}",
                    T=X,
                    R_raw=X[:3, :3],
                    cov=None,
                    convention=convention,
                    index=i,
                ),
                forward=True,
                frame_from=f"F{i}",
                frame_to=f"F{i+1}",
            )
            for i, X in enumerate(Xs)
        ]

        def walk(matrices):
            M = np.eye(4)
            for X in matrices:
                M = X @ M  # later steps leftmost
            return M

        W = walk(Xs)
        eps = 1e-6
        for k in range(4):
            cols = []
            for a in range(6):
                pert = []
                for i, X in enumerate(Xs):
                    x = eps * np.eye(6)[:, a] if i == k else np.zeros(6)
                    pert.append(X @ exp_se3(x) if convention == "right"
                                else exp_se3(x) @ X)
                Wp = walk(pert)
                if convention == "right":
                    delta = log_se3(Wp @ invert(W))
                else:
                    delta = log_se3(invert(W) @ Wp)
                cols.append(delta / eps)
            Jfd = np.column_stack(cols)
            Jan = step_jacobian(
                steps, k, convention, closed=False, T_total=W
            )
            assert np.allclose(Jfd, Jan, atol=2e-4), (
                f"{convention} k={k} Jacobian mismatch"
            )
