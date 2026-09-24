"""2-D constant-velocity Extended (here: linear) Kalman filter.

State vector ``x = [px, py, vx, vy]``.

Two observation kinds are supported:

* ``gnss`` observes position ``[px, py]``
* ``odom`` observes velocity ``[vx, vy]``

All covariance updates are numerically stabilised: after every operation the
covariance is symmetrised, projected onto the symmetric positive semi-definite
cone via an eigendecomposition clip, and a counter reports whenever that
projection had to change the matrix.  Measurement updates use the Joseph form,
which is symmetric and better conditioned than the naive ``(I-KH)P`` form.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

#: state dimension: px, py, vx, vy
STATE_DIM = 4

#: per-axis observation dimension
OBS_DIM = 2

# Eigenvalues below this magnitude are treated as numerical noise when forcing
# a matrix to be positive semi-definite.
PSD_EIG_CLIP = 1.0e-12


def transition_matrix(dt: float) -> np.ndarray:
    """Constant-velocity state transition for time step ``dt``."""
    f = np.eye(STATE_DIM)
    f[0, 2] = dt
    f[1, 3] = dt
    return f


def process_noise(dt: float, q: float) -> np.ndarray:
    """Continuous white-noise-acceleration process noise.

    ``q`` is the spectral density of acceleration noise along one axis
    (units m^2/s^3).  The state ordering is ``[px, py, vx, vy]`` so the
    discretised covariance is the 2x2-block matrix (``I_2`` per axis)::

            | A   B |        A = q*dt^3/3 I
        Q = |       |,       B = q*dt^2/2 I
            | B   C |        C = q*dt     I
    """
    if dt < 0.0:
        raise ValueError("dt must be non-negative")
    a = q * dt**3 / 3.0 * np.eye(2)
    b = q * dt**2 / 2.0 * np.eye(2)
    c = q * dt * np.eye(2)
    return np.block([[a, b], [b, c]])


def observation_matrix(kind: str) -> np.ndarray:
    """Linear observation matrix selecting position or velocity."""
    h = np.zeros((OBS_DIM, STATE_DIM), dtype=np.float64)
    if kind == "gnss":
        h[0, 0] = 1.0
        h[1, 1] = 1.0
    elif kind == "odom":
        h[0, 2] = 1.0
        h[1, 3] = 1.0
    else:  # pragma: no cover - guarded by the caller
        raise ValueError(f"unknown measurement kind: {kind!r}")
    return h


def _symmetrise(a: np.ndarray) -> np.ndarray:
    return 0.5 * (a + a.T)


def enforce_psd(a: np.ndarray, tol: float = PSD_EIG_CLIP) -> tuple[np.ndarray, int, float]:
    """Return a symmetric PSD copy of symmetric matrix ``a``.

    Uses an eigendecomposition.  Negative eigenvalues (which only arise from
    floating-point drift here) are clipped to zero.  Returns the repaired
    matrix, the count of eigenvalues that were clipped, and the most-negative
    eigenvalue seen (a diagnostic "how bad was it" number, ``0.0`` if fine).
    """
    s = _symmetrise(a)
    eigvals, eigvecs = np.linalg.eigh(s)
    min_eig = float(eigvals.min())
    clipped = int(np.count_nonzero(eigvals < tol))
    if clipped:
        eigvals = np.maximum(eigvals, 0.0)
        s = (eigvecs * eigvals) @ eigvecs.T
        s = _symmetrise(s)
    return s, clipped, min_eig


def validate_covariance(r: np.ndarray) -> str | None:
    """Validate a 2x2 measurement covariance.

    Returns ``None`` when valid, otherwise a short machine-readable reason
    code.  Shape, finiteness, symmetry and positive semi-definiteness are all
    checked against the *raw* supplied matrix (no silent repair of input).
    """
    if r is None or r.shape != (OBS_DIM, OBS_DIM):
        return "covariance_shape"
    if not np.all(np.isfinite(r)):
        return "covariance_non_finite"
    asym = float(np.max(np.abs(r - r.T)))
    if asym > 1.0e-9:
        return "covariance_not_symmetric"
    min_eig = float(np.linalg.eigvalsh(_symmetrise(r)).min())
    if min_eig < -1.0e-9:
        return "covariance_not_psd"
    return None


@dataclass
class UpdateResult:
    """Outcome of one (prediction + optional measurement) step."""

    x_pred: np.ndarray
    p_pred: np.ndarray
    z: np.ndarray | None = None
    innovation: np.ndarray | None = None
    s: np.ndarray | None = None
    nis: float | None = None
    kalman_gain: np.ndarray | None = None
    accepted: bool = False
    reject_reason: str | None = None
    # diagnostic counters from covariance stabilisation
    psd_clips_pred: int = 0
    psd_clips_upd: int = 0
    min_eig_pred: float = 0.0
    min_eig_upd: float = 0.0


@dataclass
class EKF:
    """Constant-velocity Kalman filter with stable covariance handling."""

    q: float = 1.0
    x: np.ndarray | None = None
    p: np.ndarray | None = None
    initialized: bool = False
    total_psd_clips: int = field(default=0)

    # -- initialisation ----------------------------------------------------

    def initialize(self, kind: str, z: np.ndarray, r: np.ndarray) -> None:
        """Bootstrap the state from one measurement.

        Observed axes take the measurement covariance; unobserved axes get a
        deliberately large variance so a later measurement of the other kind
        can correct them.
        """
        x = np.zeros(STATE_DIM, dtype=np.float64)
        big = 1.0e6
        p = big * np.eye(STATE_DIM, dtype=np.float64)
        if kind == "gnss":
            x[0:2] = z
            p[0, 0] = max(float(r[0, 0]), 1.0e-9)
            p[1, 1] = max(float(r[1, 1]), 1.0e-9)
        elif kind == "odom":
            x[2:4] = z
            p[2, 2] = max(float(r[0, 0]), 1.0e-9)
            p[3, 3] = max(float(r[1, 1]), 1.0e-9)
        else:  # pragma: no cover
            raise ValueError(f"unknown measurement kind: {kind!r}")
        self.x = x
        self.p, clips, _ = enforce_psd(p)
        self.total_psd_clips += clips
        self.initialized = True

    # -- core equations ----------------------------------------------------

    def predict(self, dt: float) -> tuple[np.ndarray, np.ndarray, int, float]:
        """Propagate state and covariance forward by ``dt`` seconds."""
        if not self.initialized:
            raise RuntimeError("filter is not initialized")
        f = transition_matrix(dt)
        x_pred = f @ self.x
        p_pred = f @ self.p @ f.T + process_noise(dt, self.q)
        p_pred, clips, min_eig = enforce_psd(p_pred)
        self.total_psd_clips += clips
        self.x = x_pred
        self.p = p_pred
        return x_pred, p_pred, clips, min_eig

    def update(
        self,
        kind: str,
        z: np.ndarray,
        r: np.ndarray,
        gate: float,
        x_pred: np.ndarray,
        p_pred: np.ndarray,
    ) -> UpdateResult:
        """Gate and (if accepted) apply a measurement update.

        The innovation covariance is formed with Cholesky-based inversion and
        the state covariance uses the Joseph form.  A measurement whose
        normalised innovation squared exceeds ``gate`` is rejected *before*
        touching the state, leaving the predicted state/covariance in place.
        """
        h = observation_matrix(kind)
        z_hat = h @ x_pred
        innovation = z - z_hat
        s = h @ p_pred @ h.T + r
        s, s_clips, _ = enforce_psd(s)

        # Solve S^{-1} y robustly.  Regularise slightly if Cholesky fails.
        try:
            sinv_y = np.linalg.solve(s, innovation)
        except np.linalg.LinAlgError:
            s_reg = s + 1.0e-9 * np.eye(OBS_DIM)
            sinv_y = np.linalg.solve(s_reg, innovation)
        nis = float(innovation @ sinv_y)

        res = UpdateResult(
            x_pred=x_pred.copy(),
            p_pred=p_pred.copy(),
            z=z.copy(),
            innovation=innovation,
            s=s,
            nis=nis,
        )

        if nis > gate:
            res.accepted = False
            res.reject_reason = "outlier_gate"
            return res

        # Kalman gain K = P H^T S^{-1}, solved stably against S.
        try:
            k = p_pred @ h.T @ np.linalg.solve(s, np.eye(OBS_DIM))
        except np.linalg.LinAlgError:
            k = p_pred @ h.T @ np.linalg.pinv(s)

        x_upd = x_pred + k @ innovation
        # Joseph form: P = (I-KH) P (I-KH)^T + K R K^T  (symmetric, PSD-safe)
        i_kh = np.eye(STATE_DIM) - k @ h
        p_upd = i_kh @ p_pred @ i_kh.T + k @ r @ k.T
        p_upd, clips, min_eig = enforce_psd(p_upd)
        self.total_psd_clips += clips

        self.x = x_upd
        self.p = p_upd

        res.accepted = True
        res.kalman_gain = k
        res.psd_clips_upd = clips
        res.min_eig_upd = min_eig
        return res
