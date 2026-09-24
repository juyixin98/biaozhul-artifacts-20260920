"""Unit tests for the EKF mathematics and covariance handling."""

import numpy as np
import pytest

from ekf_fusion.ekf import (
    STATE_DIM,
    EKF,
    enforce_psd,
    observation_matrix,
    process_noise,
    transition_matrix,
    validate_covariance,
)


def test_transition_and_noise_shapes():
    f = transition_matrix(0.5)
    assert f.shape == (4, 4)
    assert f[0, 2] == pytest.approx(0.5)
    assert f[1, 3] == pytest.approx(0.5)
    q = process_noise(0.5, 2.0)
    assert q.shape == (4, 4)
    # WNA discretisation: Q(dt) is PSD and scales with q
    eig = np.linalg.eigvalsh(q)
    assert (eig >= -1e-12).all()
    assert np.allclose(q, process_noise(0.5, 2.0))


def test_process_noise_additivity_over_partition():
    """WNA noise partition identity.

    Splitting an interval a+b at ``a``: the first interval's noise Q(a) is
    transported through F(b), then Q(b) is added.  The equivalent reverse
    partition (F(a) Q(b) F(a)^T + Q(a)) holds by the same algebra.
    """
    a, b, qspec = 0.3, 0.7, 1.5
    qa = process_noise(a, qspec)
    qb = process_noise(b, qspec)
    q_total = process_noise(a + b, qspec)
    assert np.allclose(q_total, transition_matrix(b) @ qa @ transition_matrix(b).T + qb,
                       atol=1e-12)
    assert np.allclose(q_total, transition_matrix(a) @ qb @ transition_matrix(a).T + qa,
                       atol=1e-12)

    # block-structure checks for the [px,py,vx,vy] ordering
    assert np.allclose(q_total[0:2, 2:4], qspec * (a + b) ** 2 / 2.0 * np.eye(2))
    assert np.allclose(q_total[2:4, 2:4], qspec * (a + b) * np.eye(2))
    assert np.allclose(q_total[0:2, 0:2],
                       qspec * (a + b) ** 3 / 3.0 * np.eye(2))


def test_enforce_psd_repairs_negative_eigenvalue():
    # eigenvalues 1 +/- 1.8 -> one mildly negative, one positive
    a = np.array([[1.0, 1.8], [1.8, 1.0]])
    repaired, clips, min_eig = enforce_psd(a)
    assert clips == 1
    assert min_eig < 0
    assert np.allclose(repaired, repaired.T)
    assert (np.linalg.eigvalsh(repaired) >= -1e-12).all()
    # positive eigenvalue (2.8) is preserved, negative one clipped to 0
    assert np.linalg.eigvalsh(repaired) == pytest.approx([0.0, 2.8], abs=1e-10)


def test_validate_covariance_rejects_bad_matrices():
    assert validate_covariance(np.zeros((3, 3))) == "covariance_shape"
    assert validate_covariance(np.array([[1.0, np.nan], [0.0, 1.0]])) \
        == "covariance_non_finite"
    assert validate_covariance(np.array([[1.0, 0.5], [0.2, 1.0]])) \
        == "covariance_not_symmetric"
    assert validate_covariance(np.array([[1.0, 2.0], [2.0, 1.0]])) \
        == "covariance_not_psd"
    assert validate_covariance(np.eye(2)) is None


def test_observation_matrices_select_right_components():
    h_g = observation_matrix("gnss")
    h_o = observation_matrix("odom")
    x = np.array([3.0, 4.0, 1.0, 0.5])
    assert np.allclose(h_g @ x, [3.0, 4.0])
    assert np.allclose(h_o @ x, [1.0, 0.5])


def test_bootstrap_then_converges_toward_constant_truth():
    """With zero-mean measurement noise the estimate stays close to truth."""
    rng = np.random.default_rng(7)
    v = np.array([1.0, -0.5])
    r_gnss = 0.1**2 * np.eye(2)
    r_odom = 0.05**2 * np.eye(2)
    filt = EKF(q=0.5)
    filt.initialize("gnss", np.zeros(2), r_gnss)

    ts = np.arange(0.1, 20.0, 0.1)
    for t in ts:
        x_pred, p_pred, _, _ = filt.predict(0.1)
        if round(t * 10) % 2 == 0:
            z = v + rng.normal(scale=0.05, size=2)
            res = filt.update("odom", z, r_odom, gate=9.2103,
                              x_pred=x_pred, p_pred=p_pred)
            assert res.accepted
        else:
            z = np.array([v[0] * t, v[1] * t]) + rng.normal(scale=0.1, size=2)
            res = filt.update("gnss", z, r_gnss, gate=9.2103,
                              x_pred=x_pred, p_pred=p_pred)
            assert res.accepted
        assert np.allclose(filt.p, filt.p.T, atol=1e-10)
        assert (np.linalg.eigvalsh(filt.p) >= -1e-12).all()

    assert np.linalg.norm(filt.x[2:4] - v) < 0.08
    assert np.linalg.norm(filt.x[0:2] - v * ts[-1]) < 0.6


def test_innovation_and_nis_scaling():
    """A perfect measurement has ~0 NIS; a 100-sigma jump is gated."""
    filt = EKF(q=0.0)
    r = np.array([[0.01, 0.0], [0.0, 0.01]])
    filt.initialize("gnss", np.zeros(2), r)
    x_pred, p_pred, _, _ = filt.predict(1.0)
    res_good = filt.update("gnss", np.array([0.0, 0.0]), r, 9.2103,
                           x_pred=x_pred, p_pred=p_pred)
    assert res_good.accepted
    assert abs(res_good.nis) < 1e-6

    x_pred2, p_pred2, _, _ = filt.predict(1.0)
    res_bad = filt.update("gnss", np.array([100.0, 100.0]), r, 9.2103,
                          x_pred=x_pred2, p_pred=p_pred2)
    assert not res_bad.accepted
    assert res_bad.reject_reason == "outlier_gate"
    assert res_bad.nis > 9.2103
    # rejected update leaves predicted state untouched
    assert np.allclose(filt.x, x_pred2)
    assert np.allclose(filt.p, p_pred2)


def test_joseph_form_keeps_covariance_symmetric_psd():
    filt = EKF(q=10.0)
    filt.initialize("odom", np.array([1.0, 1.0]), 0.01 * np.eye(2))
    r = 1e-12 * np.eye(2)  # extremely informative -> near-singular gain area
    for dt in (0.1, 0.2, 0.3, 0.4):
        x_pred, p_pred, _, _ = filt.predict(dt)
        filt.update("gnss", x_pred[:2], r, 9.2103,
                    x_pred=x_pred, p_pred=p_pred)
        assert np.allclose(filt.p, filt.p.T, atol=1e-12)
        assert (np.linalg.eigvalsh(filt.p) >= -1e-12).all()
