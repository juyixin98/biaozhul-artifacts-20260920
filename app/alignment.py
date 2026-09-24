"""Rigid / similarity alignment of two 3-D point sets (Umeyama, 1991).

Finds ``s``, ``R``, ``t`` minimising ``sum_i ||gt_i - (s R est_i + t)||^2``.

Degenerate configurations (too few points, collinear point sets, or a
zero-variance scale estimate) are rejected explicitly instead of returning an
arbitrary transform whose small error would be meaningless.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .errors import EvaluationError

# Second-largest singular value of a centred point set, relative to its
# largest one, must stay above this. Rank >= 2 (planar) already fixes an
# SE(3)/Sim(3) alignment uniquely; rank <= 1 (collinear) leaves the rotation
# around the line undetermined and is therefore rejected as degenerate.
_RANK_RATIO = 1e-9


@dataclass(frozen=True)
class Sim3Transform:
    rotation: np.ndarray  # (3, 3)
    translation: np.ndarray  # (3,)
    scale: float  # 1.0 when with_scale=False

    def apply(self, points: np.ndarray) -> np.ndarray:
        return (self.scale * points @ self.rotation.T) + self.translation


def _degeneracy_report(src_centered: np.ndarray, tgt_centered: np.ndarray) -> None:
    for name, c in (("estimated", src_centered), ("ground_truth", tgt_centered)):
        sv = np.linalg.svd(c, compute_uv=False)
        ratio = float(sv[1] / sv[0]) if sv[0] > 0.0 else 0.0
        if sv[0] <= 0.0 or ratio < _RANK_RATIO:
            raise EvaluationError(
                "ALIGNMENT_DEGENERATE",
                f"The matched {name} positions are (numerically) collinear or have "
                "zero extent; rigid-body alignment is not uniquely determined.",
                {"track": name, "singular_values": [float(x) for x in sv]},
            )


def umeyama(src: np.ndarray, dst: np.ndarray, with_scale: bool) -> Sim3Transform:
    """Align ``src`` points onto ``dst`` points (both (N, 3), one row per point)."""
    n = src.shape[0]
    if n < 2:
        raise EvaluationError(
            "ALIGNMENT_DEGENERATE",
            "At least two matched positions are required for rigid-body alignment.",
            {"matched_points": int(n)},
        )

    src_mean = src.mean(axis=0)
    dst_mean = dst.mean(axis=0)
    src_c = src - src_mean
    dst_c = dst - dst_mean
    _degeneracy_report(src_c, dst_c)

    src_var = float(np.einsum("ij,ij->", src_c, src_c) / n)  # mean squared spread
    if src_var <= 0.0:
        raise EvaluationError(
            "ALIGNMENT_DEGENERATE",
            "Estimated positions have zero variance; scale is undefined.",
            {"source_variance": src_var},
        )

    sigma = (dst_c.T @ src_c) / n  # cross-covariance
    U, D, Vt = np.linalg.svd(sigma)

    S = np.eye(3, dtype=np.float64)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0.0:
        # Prevent a reflection: flip the least-significant singular direction.
        S[2, 2] = -1.0

    R = U @ S @ Vt
    if with_scale:
        s = float(np.trace(np.diag(D) @ S) / src_var)
        if s <= 0.0 or not np.isfinite(s):
            raise EvaluationError(
                "ALIGNMENT_DEGENERATE",
                f"Estimated similarity scale is invalid (s={s:.6g}); the point "
                "configuration cannot support scale alignment.",
                {"scale": s},
            )
    else:
        s = 1.0

    t = dst_mean - s * R @ src_mean
    return Sim3Transform(rotation=R, translation=t, scale=s)
