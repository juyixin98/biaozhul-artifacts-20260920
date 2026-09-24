"""SE(3) / Sim(3) alignment of matched pose pairs (Umeyama 1991).

The estimate trajectory is mapped into the ground-truth frame:

    p_aligned = s * R @ p_est + t        (similarity)
    p_aligned = R @ p_est + t            (rigid, s = 1)

Rigid alignment is the metric benchmark (ATE in the GT metric scale);
scale alignment is an *opt-in* post-processing variant and is always
reported separately so its numbers never get mixed with rigid metrics.

Rotation alignment uses quaternion averaging (Wahba's problem), which
needs at least two non-collinear directions in the cross-covariance.
Translations are fit by least squares on rotated points; an all-coincident
point set is a degenerate request and is rejected, never silently accepted.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .errors import DegenerateAlignmentError
from .rotation import matrix_to_quat, quat_to_matrix

_EPS = 1e-12


@dataclass(frozen=True)
class AlignmentResult:
    rotation: np.ndarray  # 3x3, applied to estimate points
    translation: np.ndarray  # shape (3,)
    scale: float  # 1.0 for rigid mode; fitted (>0) for similarity mode
    with_scale: bool
    scale_drift: float  # |s - 1|, shown only in the scale report
    rms_raw: float  # RMS residual before alignment, diagnostic


def _fit_rotation(P_centered: np.ndarray, Q_centered: np.ndarray) -> np.ndarray:
    """Wahba solution R minimizing sum ||R p_i - q_i||^2 (quaternion method).

    The cross-covariance used with the Davenport K-matrix is
    ``H = Pc^T Qc`` (source ``p`` = estimate, target ``q`` = GT).

    Requires the 3x3 cross-covariance to have rank >= 2 (at least two
    non-collinear centered directions). Rank <= 1 means rotation about the
    degenerate axis(es) is unobservable -- an error, not an arbitrary guess.
    """
    H = P_centered.T @ Q_centered
    # SVD rank check on the cross-covariance.
    sv = np.linalg.svd(H, compute_uv=False)
    rank = int(np.count_nonzero(sv > 1e-10 * max(sv[0], 1.0)))
    if rank < 2:
        raise DegenerateAlignmentError(
            "rotation alignment is degenerate: matched positions span "
            f"fewer than two directions (singular values {np.round(sv, 9)})"
        )

    K = np.zeros((4, 4), dtype=np.float64)
    K[0, 0] = H[0, 0] + H[1, 1] + H[2, 2]
    K[0, 1] = K[1, 0] = H[1, 2] - H[2, 1]
    K[0, 2] = K[2, 0] = H[2, 0] - H[0, 2]
    K[0, 3] = K[3, 0] = H[0, 1] - H[1, 0]
    K[1, 1] = H[0, 0] - H[1, 1] - H[2, 2]
    K[1, 2] = K[2, 1] = H[0, 1] + H[1, 0]
    K[1, 3] = K[3, 1] = H[0, 2] + H[2, 0]
    K[2, 2] = -H[0, 0] + H[1, 1] - H[2, 2]
    K[2, 3] = K[3, 2] = H[1, 2] + H[2, 1]
    K[3, 3] = -H[0, 0] - H[1, 1] + H[2, 2]

    eigvals, eigvecs = np.linalg.eigh(K)
    q = eigvecs[:, int(np.argmax(eigvals))]
    R = quat_to_matrix(q)
    if np.linalg.det(R) < 0.0:  # reflection guard (Umeyama det correction)
        # Standard Umeyama SVD uses H' = Qc^T Pc: R = U D V^T with the
        # last singular value signed so det(R) = +1.
        Up, _sp, Vtp = np.linalg.svd(Q_centered.T @ P_centered)
        D = np.eye(3)
        D[2, 2] = np.sign(np.linalg.det(Up @ Vtp))
        R = Up @ D @ Vtp
    return R


def align_trajectories(
    est_pos: np.ndarray,
    gt_pos: np.ndarray,
    with_scale: bool = False,
) -> AlignmentResult:
    """Fit SE(3) (``with_scale=False``) or Sim(3) (``True``) est -> gt.

    Degenerate cases raise ``DegenerateAlignmentError``:
    * fewer than 2 matched points;
    * all matched estimate points coincident (variance ~ 0) -- the scale
      is undefined and rotation cannot be recovered;
    * cross-covariance rank < 2 (collinear centered geometry).
    """
    P = np.asarray(est_pos, dtype=np.float64)
    Q = np.asarray(gt_pos, dtype=np.float64)
    n = P.shape[0]
    if n < 2:
        raise DegenerateAlignmentError(
            f"need at least 2 matched points for alignment, got {n}"
        )

    mu_p = P.mean(axis=0)
    mu_q = Q.mean(axis=0)
    Pc = P - mu_p
    Qc = Q - mu_q

    var_p = float((Pc ** 2).sum() / n)
    if var_p <= _EPS:
        raise DegenerateAlignmentError(
            "all matched estimate positions are coincident: rotation and "
            "scale are not identifiable"
        )

    R = _fit_rotation(Pc, Qc)

    if with_scale:
        # Umeyama scale for map p -> q: trace(R^T H) / var_p, with
        # H = Qc^T Pc / n; positive when R is a proper rotation.
        H = Qc.T @ Pc / n
        s = float(np.trace(R.T @ H) / var_p)
        if not np.isfinite(s) or s <= _EPS:
            raise DegenerateAlignmentError(
                f"fitted scale is non-positive (s={s}): cannot perform a "
                "similarity alignment"
            )
    else:
        s = 1.0

    t = mu_q - s * (R @ mu_p)
    residual = (s * (P @ R.T) + t) - Q
    rms_raw = float(np.sqrt(max(((P - Q) ** 2).sum(axis=1).mean(), 0.0)))
    return AlignmentResult(
        rotation=R,
        translation=t,
        scale=s,
        with_scale=with_scale,
        scale_drift=abs(s - 1.0),
        rms_raw=rms_raw,
    )
