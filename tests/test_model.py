"""Model-level tests: matrices come from the ZOH double-integrator model."""

import numpy as np

from app.disturbance import deterministic_acceleration
from app.model import STATE_DIM, DoubleIntegrator


def test_zoh_matrices_exact_for_model():
    dt = 0.25
    plant = DoubleIntegrator(dt)
    np.testing.assert_allclose(
        plant.A, [[1, dt], [0, 1]], atol=1e-15
    )
    np.testing.assert_allclose(
        plant.B, [[0.5 * dt**2], [dt]], atol=1e-15
    )
    assert plant.n == STATE_DIM == 2


def test_analytic_state_update():
    plant = DoubleIntegrator(0.1)
    # p+ = p + dt v + 0.5 dt^2 u ; v+ = v + dt u
    x = plant.step([1.0, 0.5], 2.0)
    np.testing.assert_allclose(
        x, [1.0 + 0.1 * 0.5 + 0.5 * 0.01 * 2.0, 0.5 + 0.1 * 2.0]
    )


def test_disturbance_enters_through_B_and_is_bounded():
    plant = DoubleIntegrator(0.1)
    amp = 0.3
    for k in range(50):
        d = deterministic_acceleration(k, amp, seed=7)
        assert abs(d) <= amp + 1e-12
        # external acceleration acts exactly like input through B
        x = plant.step(np.zeros(2), 0.0, d)
        np.testing.assert_allclose(x, plant.B.reshape(2) * d)


def test_disturbance_is_deterministic():
    a = [deterministic_acceleration(k, 0.5, 3) for k in range(40)]
    b = [deterministic_acceleration(k, 0.5, 3) for k in range(40)]
    assert a == b
    # different seed -> different signal, same bound
    c = [deterministic_acceleration(k, 0.5, 4) for k in range(40)]
    assert a != c
    assert deterministic_acceleration(123, 0.0, 3) == 0.0


def test_rollout_shape():
    plant = DoubleIntegrator(0.1)
    states = plant.rollout([0, 0], np.full(5, 1.0))
    assert states.shape == (6, 2)
