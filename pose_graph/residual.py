"""SE2 relative-pose residual and its analytic Gauss-Newton Jacobians.

For an edge ``i -> j`` with relative measurement ``z = (zx, zy, ztheta)`` the
residual is

    e_xy  = R(-theta_i) (t_j - t_i) - z_xy
    e_th  = wrap(theta_j - theta_i - ztheta)

with 3x3 Jacobian blocks ``A = de/d pose_i`` and ``B = de/d pose_j``.

The angular residual is normalized to ``[-pi, pi)`` on every evaluation, so a
measurement / state near the +/-pi cut converges in the correct direction
(the Jacobian is locally +/-1; the wrapping itself is piecewise smooth).
"""

import numpy as np

from .se2 import inverse_rotation_matrix, wrap_angle


def edge_residual_and_jacobians(pose_i, pose_j, z):
    """Return ``(residual(3,), A(3,3), B(3,3))`` for one edge."""
    xi, yi, ti = pose_i
    xj, yj, tj = pose_j

    r_inv = inverse_rotation_matrix(ti)          # R(-ti): world -> frame i
    delta = np.array([xj - xi, yj - yi])
    predicted_xy = r_inv @ delta

    # d R(-ti) / d theta_i  (direct derivative of [[c,s],[-s,c]])
    c, s = np.cos(ti), np.sin(ti)
    dr_dti = np.array([[-s, c], [-c, -s]]) @ delta

    residual = np.array(
        [
            predicted_xy[0] - z[0],
            predicted_xy[1] - z[1],
            wrap_angle(tj - ti - z[2]),
        ]
    )

    a = np.zeros((3, 3))
    a[0:2, 0:2] = -r_inv
    a[0:2, 2] = dr_dti
    a[2, 2] = -1.0

    b = np.zeros((3, 3))
    b[0:2, 0:2] = r_inv
    b[2, 2] = 1.0
    return residual, a, b


def edge_residual(pose_i, pose_j, z):
    """Residual only (cheap cost/line-search evaluations)."""
    xi, yi, ti = pose_i
    xj, yj, tj = pose_j
    r_inv = inverse_rotation_matrix(ti)
    predicted_xy = r_inv @ np.array([xj - xi, yj - yi])
    return np.array(
        [
            predicted_xy[0] - z[0],
            predicted_xy[1] - z[1],
            wrap_angle(tj - ti - z[2]),
        ]
    )
