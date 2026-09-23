"""Tests for the constant-velocity Kalman filter: time-dependent transition,
prediction accuracy and that a later measurement reduces uncertainty."""

import numpy as np

from app.mot.kalman import KalmanBox2D, process_noise, transition_matrix


def test_transition_matrix_uses_dt():
    f1 = transition_matrix(1.0)
    f2 = transition_matrix(2.0)
    assert f1[0, 2] == 1.0
    assert f2[0, 2] == 2.0
    np.testing.assert_allclose(f1 @ f1, f2)


def test_process_noise_scales_with_dt():
    q1 = process_noise(1.0, q=0.5)
    q2 = process_noise(2.0, q=0.5)
    assert q2[0, 0] > q1[0, 0]
    assert np.all(np.linalg.eigvalsh(q1) >= 0)
    assert np.all(np.linalg.eigvalsh(q2) >= 0)


def test_constant_velocity_prediction_tracks_line():
    kf = KalmanBox2D((0.0, 0.0), q=0.01, r=0.0001)
    # Learn the velocity from several exact observations spaced 0.1 s.
    for k in range(1, 8):
        kf.predict(0.1)
        kf.update((1.0 * 0.1 * k, 0.5 * 0.1 * k))
    kf.predict(0.1)
    pos = kf.position
    np.testing.assert_allclose(pos, [0.8, 0.4], atol=0.02)
    np.testing.assert_allclose(kf.velocity, [1.0, 0.5], atol=0.1)


def test_update_reduces_position_variance():
    kf = KalmanBox2D((0.0, 0.0))
    kf.predict(0.1)
    p_before = kf.P[0, 0]
    kf.update((0.2, 0.1))
    assert kf.P[0, 0] < p_before


def test_negative_dt_rejected():
    kf = KalmanBox2D((0.0, 0.0))
    try:
        kf.predict(-1.0)
    except ValueError:
        return
    raise AssertionError("negative dt must raise")


def test_mahalanobis_zero_at_prediction():
    kf = KalmanBox2D((1.0, 2.0))
    kf.predict(0.1)
    assert kf.mahalanobis(kf.position) < 1e-12
