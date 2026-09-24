"""Finite-difference validation of every analytic Jacobian the optimizer uses."""
import numpy as np
import pytest

from app.geometry import Rect, rect_sdf_gradients, rect_signed_distance
from app.smoother import _diff_matrix, _menger_curvature_and_jac

EPS = 1e-7


def fd(f, x, eps=EPS):
    out = np.empty_like(x, dtype=float)
    for i in range(len(x)):
        xp, xm = x.copy(), x.copy()
        xp[i] += eps
        xm[i] -= eps
        out[i] = (f(xp) - f(xm)) / (2 * eps)
    return out


def test_menger_curvature_values():
    # Circle of radius R=5 sampled at equal angles -> curvature ~ 0.2
    theta = np.linspace(0, 1.0, 5)
    R = 5.0
    P = np.column_stack([R * np.cos(theta), R * np.sin(theta)])
    k, J = _menger_curvature_and_jac(P)
    assert np.all(k > 0)
    assert np.allclose(k, 1.0 / R, rtol=2e-2)
    # straight collinear points -> zero curvature
    line = np.array([[0.0, 0], [1, 0], [2, 0], [3, 0]])
    k2, _ = _menger_curvature_and_jac(line)
    assert np.allclose(k2, 0.0, atol=1e-12)


def test_menger_jacobian_finite_difference():
    rng = np.random.default_rng(3)
    P = rng.normal(size=(7, 2))
    k, J = _menger_curvature_and_jac(P)
    flat = P.ravel()
    Jfd = np.zeros_like(J)
    for i in range(len(flat)):
        fp, fm = flat.copy(), flat.copy()
        fp[i] += EPS
        fm[i] -= EPS
        Jfd[:, i] = (_menger_curvature_and_jac(fp.reshape(-1, 2))[0]
                     - _menger_curvature_and_jac(fm.reshape(-1, 2))[0]) / (2 * EPS)
    denom = 1.0 + np.abs(Jfd)
    assert np.max(np.abs(J - Jfd) / denom) < 1e-6


def test_diff_matrices():
    m = 9
    x = np.linspace(-1.0, 2.0, m)
    D2 = _diff_matrix(m, 2)
    D3 = _diff_matrix(m, 3)
    assert D2.shape == (m - 2, m)
    assert D3.shape == (m - 3, m)
    assert np.allclose(D2 @ x, x[2:] - 2 * x[1:-1] + x[:-2])
    assert np.allclose(D3 @ x, x[3:] - 3 * x[2:-1] + 3 * x[1:-2] - x[:-3])


def test_assembled_clearance_jacobian():
    # Mirror the production assembly on a tiny problem and FD-check it.
    m, nI = 5, 3
    scale = 10.0
    P0 = np.array([(0, 0), (2, 0.5), (4, -0.5), (6, 0.3), (8, 0)], float) / scale
    rects_n = [Rect.from_center_wh(0.3, -0.09, 0.2, 0.1),
               Rect.from_center_wh(0.6, 0.09, 0.2, 0.1)]
    B = np.zeros((m, nI))
    B[np.arange(1, m - 1), np.arange(nI)] = 1.0
    end = np.zeros((m, 2))
    end[0], end[-1] = P0[0], P0[-1]
    interior = np.arange(1, m - 1)
    mids = np.zeros((m - 1, m))
    mids[np.arange(m - 1), np.arange(m - 1)] = 0.5
    mids[np.arange(m - 1), np.arange(1, m)] = 0.5
    C = np.vstack([np.eye(m), mids])
    CI = C[:, interior]

    def unpack(z):
        P = np.empty((m, 2))
        P[:, 0] = B @ z[:nI] + end[:, 0]
        P[:, 1] = B @ z[nI:] + end[:, 1]
        return P

    def con(z):
        S = C @ unpack(z)
        cs, blocks = [], []
        for r in rects_n:
            g = rect_sdf_gradients(S, r)
            cs.append(rect_signed_distance(S, r) - 0.005)
            jx = g[:, 0:1] * CI
            jy = g[:, 1:2] * CI
            blocks.append(np.hstack([jx, jy]))
        return np.concatenate(cs), np.vstack(blocks)

    z = np.array([0.01, -0.02, 0.015, 0.0, -0.01, 0.02])
    c, J = con(z)
    Jfd = np.zeros_like(J)
    for i in range(len(z)):
        zp, zm = z.copy(), z.copy()
        zp[i] += EPS
        zm[i] -= EPS
        Jfd[:, i] = (con(zp)[0] - con(zm)[0]) / (2 * EPS)
    assert J.shape == (2 * C.shape[0], 2 * nI)
    assert np.max(np.abs(J - Jfd) / (1 + np.abs(Jfd))) < 1e-6


@pytest.mark.parametrize("inside", [False, True])
def test_sdf_gradient_all_quadrants(inside):
    r = Rect.from_center_wh(0.0, 0.0, 2.0, 2.0)
    rng = np.random.default_rng(11)
    if inside:
        S = rng.uniform(-0.9, 0.9, size=(300, 2))
    else:
        S = rng.uniform(-3.0, 3.0, size=(300, 2))
        S = S[np.maximum(np.abs(S[:, 0]) - 1, np.abs(S[:, 1]) - 1) > 0.05]
    g = rect_sdf_gradients(S, r)
    for j in range(S.shape[0]):
        for axis in range(2):
            Sp, Sm = S.copy(), S.copy()
            Sp[j, axis] += EPS
            Sm[j, axis] -= EPS
            want = (rect_signed_distance(Sp, r)[j]
                    - rect_signed_distance(Sm, r)[j]) / (2 * EPS)
            assert g[j, axis] == pytest.approx(want, abs=1e-5)
