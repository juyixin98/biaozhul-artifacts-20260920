"""Constant-velocity 2-D Kalman filter.

State vector ``x = [px, py, vx, vy]^T``; measurements are positions ``z = [px, py]^T``.
The state transition explicitly depends on the elapsed time ``dt``:

    F(dt) = [[1, 0, dt, 0],
             [0, 1, 0, dt],
             [0, 0, 1,  0],
             [0, 0, 0,  1]]

Continuous white-noise acceleration is assumed, so the process-noise
covariance ``Q(dt)`` scales with ``q`` (process noise intensity) and the
elapsed time.  All matrix operations are real NumPy linear algebra.
"""

from __future__ import annotations

import numpy as np

STATE_DIM = 4
MEAS_DIM = 2


def transition_matrix(dt: float) -> np.ndarray:
    """Return the constant-velocity transition matrix for time step ``dt``."""
    f = np.eye(STATE_DIM, dtype=float)
    f[0, 2] = dt
    f[1, 3] = dt
    return f


def process_noise(dt: float, q: float) -> np.ndarray:
    """Process-noise covariance under continuous white-noise acceleration.

    Derived from integrating an acceleration noise input ``q`` over ``dt``;
    position variance grows as ``dt**3 / 3`` and the position/velocity
    cross term as ``dt**2 / 2``.
    """
    q_mat = np.zeros((STATE_DIM, STATE_DIM), dtype=float)
    dt2 = dt * dt
    dt3 = dt2 * dt
    # x / px block, then identical y / py block
    q_mat[0, 0] = dt3 / 3.0
    q_mat[0, 2] = dt2 / 2.0
    q_mat[2, 0] = dt2 / 2.0
    q_mat[2, 2] = dt
    q_mat[1, 1] = dt3 / 3.0
    q_mat[1, 3] = dt2 / 2.0
    q_mat[3, 1] = dt2 / 2.0
    q_mat[3, 3] = dt
    return q * q_mat


_MEAS_MATRIX = np.array(
    [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]], dtype=float
)


class KalmanBox2D:
    """A single 2-D constant-velocity Kalman track state.

    Parameters
    ----------
    z0:
        First position measurement ``(px, py)``.
    q:
        Process-noise intensity (acceleration noise power).
    r:
        Measurement-noise variance per axis (isotropic ``R = r * I``).
    init_vel_var:
        Initial velocity variance (large value => velocity initially unknown).
    """

    def __init__(
        self,
        z0: tuple[float, float] | np.ndarray,
        q: float = 1.0,
        r: float = 0.05**2,
        init_vel_var: float = 100.0,
    ) -> None:
        self.q = float(q)
        self.r = float(r)
        self.x = np.array([z0[0], z0[1], 0.0, 0.0], dtype=float)
        self.P = np.diag([self.r, self.r, init_vel_var, init_vel_var]).astype(float)
        # Innovation of the most recent update (None before the first update).
        self.S: np.ndarray | None = None

    @property
    def position(self) -> np.ndarray:
        return self.x[:2].copy()

    @property
    def velocity(self) -> np.ndarray:
        return self.x[2:4].copy()

    def predict(self, dt: float) -> np.ndarray:
        """Advance the state by ``dt`` seconds; return the predicted position."""
        if dt < 0:
            raise ValueError(f"dt must be non-negative, got {dt}")
        f = transition_matrix(dt)
        self.x = f @ self.x
        self.P = f @ self.P @ f.T + process_noise(dt, self.q)
        # Symmetrize to fight round-off asymmetry.
        self.P = 0.5 * (self.P + self.P.T)
        return self.position

    def innovation_covariance(self) -> np.ndarray:
        """Innovation covariance ``S = H P H^T + R`` at the current (predicted) time."""
        h = _MEAS_MATRIX
        return h @ self.P @ h.T + self.r * np.eye(MEAS_DIM)

    def update(self, z: tuple[float, float] | np.ndarray) -> np.ndarray:
        """Correct the state with measurement ``z``; return the innovation ``y``."""
        h = _MEAS_MATRIX
        z = np.asarray(z, dtype=float)
        self.S = self.innovation_covariance()
        # Kalman gain K = P H^T S^{-1}
        k = self.P @ h.T @ np.linalg.inv(self.S)
        y = z - h @ self.x
        self.x = self.x + k @ y
        # Joseph form covariance update (symmetric, positive-semidefinite).
        i_kh = np.eye(STATE_DIM) - k @ h
        self.P = i_kh @ self.P @ i_kh.T + k @ (self.r * np.eye(MEAS_DIM)) @ k.T
        self.P = 0.5 * (self.P + self.P.T)
        return y

    def mahalanobis(self, z: tuple[float, float] | np.ndarray) -> float:
        """Squared Mahalanobis distance ``y^T S^{-1} y`` of ``z`` to the prediction."""
        s = self.innovation_covariance()
        y = np.asarray(z, dtype=float) - self.position
        return float(y @ np.linalg.solve(s, y))
