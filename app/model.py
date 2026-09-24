"""Double-integrator plant model.

Continuous-time 1-D point mass with state ``x = [position, velocity]`` and
scalar control ``u = acceleration``::

    p_ddot = u

Zero-order-hold discretisation with sample time ``dt`` gives the exact
discrete model::

    A = [[1, dt], [0, 1]]      B = [[dt^2 / 2], [dt]]
    x_{k+1} = A x_k + B (u_k + d_k)

``d_k`` is an optional *known-to-the-simulator* external acceleration
(synthetic disturbance).  The MPC controller never sees ``d_k`` — it is used
only by the simulation plant, exactly as a real unmeasured disturbance would
be.
"""

from __future__ import annotations

import numpy as np

STATE_DIM = 2
CONTROL_DIM = 1


class DoubleIntegrator:
    """Exact ZOH discretisation of the 1-D double integrator."""

    n = STATE_DIM
    m = CONTROL_DIM

    def __init__(self, dt: float):
        if dt <= 0:
            raise ValueError("dt must be positive")
        self.dt = float(dt)
        # Matrices are built analytically *from the model* (A_c, B_c, dt).
        # This is the exact matrix exponential solution for this system.
        self.A = np.array([[1.0, self.dt], [0.0, 1.0]], dtype=float)
        self.B = np.array([[0.5 * self.dt**2], [self.dt]], dtype=float)

    def step(
        self,
        x: np.ndarray,
        u: np.ndarray | float,
        disturbance: np.ndarray | float | None = None,
    ) -> np.ndarray:
        """Advance one time step: ``x+ = A x + B (u + d)``.

        The disturbance ``d`` is an external acceleration (same units as the
        control input).  It enters through the model's ``B`` matrix, not as an
        arbitrary state offset.
        """
        x = np.asarray(x, dtype=float).reshape(self.n)
        u = float(np.asarray(u, dtype=float).reshape(1)[0])
        d = 0.0 if disturbance is None else float(
            np.asarray(disturbance, dtype=float).reshape(1)[0]
        )
        return self.A @ x + self.B.reshape(self.n) * (u + d)

    def rollout(
        self, x0: np.ndarray, controls: np.ndarray, disturbance: np.ndarray | None = None
    ) -> np.ndarray:
        """Roll out a control sequence. Returns states with shape (N+1, n)."""
        x0 = np.asarray(x0, dtype=float).reshape(self.n)
        controls = np.asarray(controls, dtype=float).reshape(-1, self.m)
        n_steps = controls.shape[0]
        states = np.zeros((n_steps + 1, self.n))
        states[0] = x0
        for k in range(n_steps):
            d = None if disturbance is None else disturbance[k]
            states[k + 1] = self.step(states[k], controls[k], d)
        return states
