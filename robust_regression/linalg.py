"""SVD 最小二乘求解器（自行实现，不调用 np.linalg.lstsq）。

秩亏处理策略：对增广设计矩阵（含截距列）做紧 SVD，以
``max(s) * rcond`` 为阈值判定有效秩，秩亏时返回最小范数解
（Moore–Penrose 伪逆解）并通过返回值的 ``rank`` / ``rank_deficient``
明确上报。

规模限定：由 validation 层保证（默认 n <= 100000, p <= 200），
本模块只做小规模 SVD（np.linalg.svd 的经济模式）。
"""

from __future__ import annotations

import numpy as np

RCOND = 1e-10


def svd_min_norm(A: np.ndarray, b: np.ndarray, rcond: float = RCOND):
    """秩亏安全的最小二乘最小范数解。

    求解 ``min ||A β - b||``；当 rank(A) < 列数时返回最小范数解。

    Parameters
    ----------
    A, b : ndarray
    rcond : 奇异值相对阈值

    Returns
    -------
    beta : ndarray, shape (k,)
        最小范数最小二乘解。
    rank : int
        有效秩。
    rank_deficient : bool
        有效秩是否小于列数。
    """
    U, s, Vt = np.linalg.svd(A, full_matrices=False)
    tol = float(s[0]) * rcond if s.size > 0 else 0.0
    effective = s > tol
    rank = int(np.count_nonzero(effective))
    s_inv = np.zeros_like(s)
    s_inv[effective] = 1.0 / s[effective]
    beta = Vt.T * s_inv @ (U.T @ b)
    return beta, rank, bool(rank < A.shape[1])


def solve_ols(
    X: np.ndarray,
    y: np.ndarray,
    fit_intercept: bool = True,
    rcond: float = RCOND,
):
    """普通最小二乘（SVD 伪逆）。

    Returns
    -------
    dict，键：coef, intercept, beta, rank, rank_deficient
    """
    n, p = X.shape
    if fit_intercept:
        A = np.hstack([X, np.ones((n, 1), dtype=np.float64)])
    else:
        A = X
    beta, rank, rank_deficient = svd_min_norm(A, y, rcond=rcond)
    if fit_intercept:
        return {
            "coef": beta[:p].copy(),
            "intercept": float(beta[p]),
            "beta": beta,
            "rank": rank,
            "rank_deficient": rank_deficient,
        }
    return {
        "coef": beta.copy(),
        "intercept": 0.0,
        "beta": beta,
        "rank": rank,
        "rank_deficient": rank_deficient,
    }


def solve_weighted_ridge(
    X: np.ndarray,
    y: np.ndarray,
    weights: np.ndarray,
    reg_lambda: float,
    fit_intercept: bool = True,
    rcond: float = RCOND,
):
    """加权岭回归（IRLS 的内迭代求解器）。

    求解
        min_β  Σ_i w_i (y_i - [X,1]_i β)^2 + λ Σ_{j ∈ 惩罚列} β_j²

    截距列（若有）不惩罚。方法：对增广后的行加权/正则方程做一次 SVD，
    因此正则矩阵严格正定（λ > 0，惩罚列）或秩亏时仍安全；不进行
    开方、不直接求逆。

    Returns
    -------
    dict，键：beta, rank, rank_deficient
        其中 rank 为加权增广系统的有效秩（用于诊断共线性）。
    """
    n, p = X.shape
    sw = np.sqrt(np.clip(weights, 1e-12, None))
    Xw = X * sw[:, None]
    yw = y * sw
    if fit_intercept:
        ones_w = np.ones(n, dtype=np.float64) * sw
        A = np.hstack([Xw, ones_w[:, None]])
        penalty = np.full(p + 1, float(reg_lambda), dtype=np.float64)
        penalty[p] = 0.0  # 截距不惩罚
        rhs_extra = np.zeros(p + 1, dtype=np.float64)
    else:
        A = Xw
        penalty = np.full(p, float(reg_lambda), dtype=np.float64)
        rhs_extra = np.zeros(p, dtype=np.float64)
    sqrt_penalty = np.sqrt(penalty)
    # 秩诊断基于“未加岭惩罚行”的加权增广系统；若直接用 A_aug，
    # λ>0 时惩罚行使系统恒满秩，会掩盖 X 的共线性。
    diag_rank = None
    diag_rank_deficient = None
    diag_s = np.linalg.svd(A, compute_uv=False)
    if diag_s.size > 0:
        diag_tol = float(diag_s[0]) * rcond
        diag_rank = int(np.count_nonzero(diag_s > diag_tol))
        diag_rank_deficient = bool(diag_rank < A.shape[1])
    A_aug = np.vstack([A, np.diag(sqrt_penalty)])
    b_aug = np.concatenate([yw, rhs_extra])
    # 岭系统在正则列为正时非奇异；rcond 仅用于极小 λ 情形的兜底
    beta, _, _ = svd_min_norm(A_aug, b_aug, rcond=rcond)
    return {
        "beta": beta,
        "rank": diag_rank,
        "rank_deficient": diag_rank_deficient,
    }
