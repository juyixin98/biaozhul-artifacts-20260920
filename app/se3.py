"""SE(3) Lie group primitives.

Convention (used uniformly across the service):

* A pose is a rigid transform ``T = [R t; 0 1]`` representing the pose of a
  frame ``B`` measured in frame ``A`` (i.e. maps points ``p_B -> p_A``).
* Tangent vectors are ``xi = [rho; phi] in R^6``: 3 translation parts first,
  then 3 rotation (axis-angle) parts.
* Right perturbation: ``T = T_bar * Exp(xi)``.
* Left perturbation:  ``T = Exp(xi) * T_bar``.
* The adjoint ``Ad(T)`` maps tangent vectors between the two conventions:
  ``Exp(Ad(T) x) * T == T * Exp(x)``.

Everything here is plain NumPy.  Rotations use the Rodrigues formula with
branch-free small-angle expansions so the maps stay smooth near the identity.
"""

from __future__ import annotations

import numpy as np

EPS = 1.0e-10


def skew(w: np.ndarray) -> np.ndarray:
    """Skew-symmetric matrix [w]_x of a 3-vector."""
    w = np.asarray(w, dtype=float).reshape(3)
    return np.array(
        [
            [0.0, -w[2], w[1]],
            [w[2], 0.0, -w[0]],
            [-w[1], w[0], 0.0],
        ]
    )


def vee(What: np.ndarray) -> np.ndarray:
    """Inverse of :func:`skew` for a (near) skew-symmetric 3x3 matrix."""
    return 0.5 * np.array(
        [What[2, 1] - What[1, 2], What[0, 2] - What[2, 0], What[1, 0] - What[0, 1]]
    )


def exp_so3(phi: np.ndarray) -> np.ndarray:
    """Exponential map so(3) -> SO(3) (Rodrigues)."""
    phi = np.asarray(phi, dtype=float).reshape(3)
    theta = float(np.linalg.norm(phi))
    K = skew(phi)
    if theta < EPS:
        # I + K + K^2/2 + K^3/6 ...; K^3 ~ theta^3, include for smoothness
        return np.eye(3) + K + 0.5 * K @ K + (1.0 / 6.0) * K @ K @ K
    A = np.sin(theta) / theta
    B = (1.0 - np.cos(theta)) / theta**2
    return np.eye(3) + A * K + B * K @ K


def log_so3(R: np.ndarray) -> np.ndarray:
    """Logarithm SO(3) -> axis-angle vector, |phi| in [0, pi]."""
    cos_theta = float(np.clip((np.trace(R) - 1.0) * 0.5, -1.0, 1.0))
    theta = np.arccos(cos_theta)
    if theta < EPS:
        # vee((R - R^T)/2) with the leading correction of theta/(2 sin theta)
        # = 1/2 + theta^2/12 + ...
        return vee(0.5 * (R - R.T)) * (1.0 + theta**2 / 6.0)
    return (theta / (2.0 * np.sin(theta))) * vee(R - R.T)


def _left_jacobian(phi: np.ndarray) -> np.ndarray:
    """Left Jacobian V(phi) with Exp(phi, rho) = (Exp(phi), V rho)."""
    theta = float(np.linalg.norm(phi))
    K = skew(phi)
    if theta < 1.0e-4:
        # Series: I + K/2 + K^2/6 + K^3/24 ... ; include K^3 for accuracy
        # when theta is large enough to matter but small enough to skip the
        # trigonometric closed form (which cancels catastrophically).
        return np.eye(3) + 0.5 * K + (1.0 / 6.0) * K @ K + (1.0 / 24.0) * K @ K @ K
    B = (1.0 - np.cos(theta)) / theta**2
    C = (theta - np.sin(theta)) / theta**3
    return np.eye(3) + B * K + C * K @ K


def _left_jacobian_inv(phi: np.ndarray) -> np.ndarray:
    """Inverse of :func:`_left_jacobian`.

    ``V^{-1} = I - K/2 + c K^2`` with
    ``c = theta^{-2}(1 - theta sin(theta)/(2(1 - cos(theta))))``
    (positive, tending to ``+1/12``).  A series is used below the
    cancellation-prone range.
    """
    theta = float(np.linalg.norm(phi))
    K = skew(phi)
    if theta < 1.0e-4:
        return (
            np.eye(3)
            - 0.5 * K
            + (1.0 / 12.0) * K @ K
            - (1.0 / 720.0) * K @ K @ K @ K
        )
    coef = (1.0 / theta**2) * (
        1.0 - theta * np.sin(theta) / (2.0 * (1.0 - np.cos(theta)))
    )
    return np.eye(3) - 0.5 * K + coef * K @ K


def exp_se3(xi: np.ndarray) -> np.ndarray:
    """Exponential map se(3) -> SE(3), returning a 4x4 transform."""
    xi = np.asarray(xi, dtype=float).reshape(6)
    rho, phi = xi[:3], xi[3:]
    R = exp_so3(phi)
    t = _left_jacobian(phi) @ rho
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = t
    return T


def log_se3(T: np.ndarray) -> np.ndarray:
    """Logarithm SE(3) -> se(3), returning xi = [rho; phi]."""
    T = np.asarray(T, dtype=float)
    phi = log_so3(T[:3, :3])
    rho = _left_jacobian_inv(phi) @ T[:3, 3]
    return np.concatenate([rho, phi])


def make_T(R: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Assemble a 4x4 transform from rotation and translation."""
    T = np.eye(4)
    T[:3, :3] = np.asarray(R, dtype=float).reshape(3, 3)
    T[:3, 3] = np.asarray(t, dtype=float).reshape(3)
    return T


def invert(T: np.ndarray) -> np.ndarray:
    """Inverse of a rigid transform."""
    T = np.asarray(T, dtype=float)
    R = T[:3, :3]
    Ti = np.eye(4)
    Ti[:3, :3] = R.T
    Ti[:3, 3] = -R.T @ T[:3, 3]
    return Ti


def adjoint(T: np.ndarray) -> np.ndarray:
    """Adjoint Ad(T), 6x6, mapping body to spatial tangent coordinates."""
    T = np.asarray(T, dtype=float)
    R, t = T[:3, :3], T[:3, 3]
    Ad = np.zeros((6, 6))
    Ad[:3, :3] = R
    Ad[:3, 3:] = skew(t) @ R
    Ad[3:, 3:] = R
    return Ad


def quat_to_R(q: np.ndarray) -> np.ndarray:
    """Hamilton quaternion ``[w, x, y, z]`` (already validated/unit) to R."""
    w, x, y, z = (float(v) for v in np.asarray(q, dtype=float).reshape(4))
    n = np.sqrt(w * w + x * x + y * y + z * z)
    w, x, y, z = w / n, x / n, y / n, z / n
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
        ]
    )


def compose(*Ts: np.ndarray) -> np.ndarray:
    """Compose transforms left to right (T1 then T2 ...)."""
    out = np.eye(4)
    for T in Ts:
        out = out @ np.asarray(T, dtype=float)
    return out
