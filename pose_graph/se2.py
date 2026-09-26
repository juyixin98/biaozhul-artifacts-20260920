"""SE(2) pose primitives.

A pose is represented as ``(x, y, theta)`` where ``theta`` is in radians.
Angles observed by residuals are always normalized to ``[-pi, pi)`` so that
angle differences crossing the +/-pi cut are handled correctly.
"""

import numpy as np

#: 3x3 block-diagonal twist adjacency is unnecessary in SE2; keep constants here.
TWO_PI = 2.0 * np.pi


def wrap_angle(angle):
    """Normalize an angle (or array) to the half-open interval ``[-pi, pi)``.

    The endpoints +pi and -pi denote the same angle; this convention returns
    -pi at the cut, which is harmless for residual minimization.
    """
    return (np.asarray(angle) + np.pi) % TWO_PI - np.pi


def rotation_matrix(theta):
    """Rotation matrix ``R(theta)`` that rotates frame-local vectors to world."""
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[c, -s], [s, c]], dtype=float)


def inverse_rotation_matrix(theta):
    """Rotation ``R(-theta) = R(theta).T``: world vector -> frame of *theta*."""
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[c, s], [-s, c]], dtype=float)


def pose_compose(pose_a, pose_b):
    """Compose two SE2 poses: ``a (+) b``, i.e. *b* expressed in world frame."""
    xa, ya, ta = pose_a
    xb, yb, tb = pose_b
    xy = np.array([xa, ya]) + rotation_matrix(ta) @ np.array([xb, yb])
    return np.array([xy[0], xy[1], wrap_angle(ta + tb)], dtype=float)


def pose_inverse(pose):
    """Inverse of an SE2 pose."""
    x, y, theta = pose
    xy = -inverse_rotation_matrix(theta) @ np.array([x, y])
    return np.array([xy[0], xy[1], wrap_angle(-theta)], dtype=float)


def relative_pose(pose_i, pose_j):
    """Relative pose ``z_ij = i^-1 (+) j`` measured in the frame of node *i*."""
    xi, yi, ti = np.asarray(pose_i, dtype=float)
    xj, yj, tj = np.asarray(pose_j, dtype=float)
    translation = inverse_rotation_matrix(ti) @ (np.array([xj, yj]) - np.array([xi, yi]))
    return np.array([translation[0], translation[1], wrap_angle(tj - ti)], dtype=float)
