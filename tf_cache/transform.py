"""Rigid-body transforms in SE(3), built on NumPy + SciPy.

Convention: an SE3 maps points from a *child* frame into a *parent* frame,
i.e. ``p_parent = T.apply(p_child)``.  Composition follows the same rule as
matrix multiplication: ``(a * b).apply(p) == a.apply(b.apply(p))``.

Rotations are stored as unit quaternions in scalar-last (x, y, z, w) order,
matching SciPy's convention.
"""

from __future__ import annotations

import numpy as np
from scipy.spatial.transform import Rotation, Slerp


class SE3:
    """A rigid transform: rotation + translation."""

    __slots__ = ("_rot", "_trans")

    def __init__(self, rotation: Rotation, translation: np.ndarray) -> None:
        translation = np.asarray(translation, dtype=float)
        if translation.shape != (3,):
            raise ValueError(f"translation must have shape (3,), got {translation.shape}")
        self._rot = rotation
        self._trans = translation

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------
    @classmethod
    def identity(cls) -> "SE3":
        return cls(Rotation.identity(), np.zeros(3))

    @classmethod
    def from_quat_translation(cls, quat_xyzw, translation) -> "SE3":
        """Build from a quaternion [x, y, z, w] and a translation [x, y, z]."""
        quat = np.asarray(quat_xyzw, dtype=float)
        if quat.shape != (4,):
            raise ValueError(f"quaternion must have shape (4,), got {quat.shape}")
        norm = np.linalg.norm(quat)
        if norm == 0.0:
            raise ValueError("zero quaternion is not a valid rotation")
        rot = Rotation.from_quat(quat / norm)
        return cls(rot, np.asarray(translation, dtype=float))

    @classmethod
    def from_matrix(cls, mat: np.ndarray) -> "SE3":
        mat = np.asarray(mat, dtype=float)
        if mat.shape != (4, 4):
            raise ValueError(f"matrix must have shape (4, 4), got {mat.shape}")
        return cls(Rotation.from_matrix(mat[:3, :3]), mat[:3, 3].copy())

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------
    @property
    def rotation(self) -> Rotation:
        return self._rot

    @property
    def translation(self) -> np.ndarray:
        return self._trans

    def quat_xyzw(self) -> np.ndarray:
        return self._rot.as_quat()

    def as_matrix(self) -> np.ndarray:
        mat = np.eye(4)
        mat[:3, :3] = self._rot.as_matrix()
        mat[:3, 3] = self._trans
        return mat

    # ------------------------------------------------------------------
    # Algebra
    # ------------------------------------------------------------------
    def inverse(self) -> "SE3":
        rot_inv = self._rot.inv()
        return SE3(rot_inv, -rot_inv.apply(self._trans))

    def __mul__(self, other: "SE3") -> "SE3":
        """Compose: (self * other).apply(p) == self.apply(other.apply(p))."""
        if not isinstance(other, SE3):
            return NotImplemented
        rot = self._rot * other._rot
        trans = self._rot.apply(other._trans) + self._trans
        return SE3(rot, trans)

    def apply(self, points: np.ndarray) -> np.ndarray:
        return self._rot.apply(points) + self._trans

    def interpolate(self, other: "SE3", t: float) -> "SE3":
        """Interpolate between self (t=0) and other (t=1).

        Translation is linear; rotation uses SLERP.  scipy's Slerp works on
        relative rotations expressed as rotvecs (norm <= pi), so the short
        arc is always taken and a quaternion sign flip between keyframes
        (q vs -q) does not cause a spurious half-turn.
        """
        if not 0.0 <= t <= 1.0:
            raise ValueError(f"interpolation parameter must be in [0, 1], got {t}")
        trans = (1.0 - t) * self._trans + t * other._trans
        slerp = Slerp([0.0, 1.0], Rotation.concatenate([self._rot, other._rot]))
        rot = slerp([t])[0]
        return SE3(rot, trans)

    # ------------------------------------------------------------------
    # Comparison helpers (for tests and diagnostics)
    # ------------------------------------------------------------------
    def error_to(self, other: "SE3") -> tuple[float, float]:
        """Return (translation error, rotation angle error) vs ``other``."""
        d_trans = float(np.linalg.norm(self._trans - other._trans))
        d_rot = float((self._rot.inv() * other._rot).magnitude())
        return d_trans, d_rot

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SE3(quat_xyzw={self.quat_xyzw().tolist()}, translation={self._trans.tolist()})"
