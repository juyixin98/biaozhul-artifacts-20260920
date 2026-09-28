"""后验误差证据与独立参考。

提供三类机制, 全部独立于被测内核的自我声明:

1. :func:`residual_evidence` —— 逐特征对与整体残差
   ``A v - lambda v`` (绝对/相对/按特征值归一化)。
2. :func:`orthogonality_evidence` —— ``V^T V - I``。
3. :func:`reconstruction_evidence` —— ``V diag(w) V^T`` 对 A 的重构。
4. :func:`cluster_eigenvalues` / :func:`subspace_principal_angles`
   —— 重 (近重) 特征值按 **特征子空间** 比较, 而非逐向量比较符号/混合。
5. :mod:`reference` 子模块 —— 成熟库 (SciPy LAPACK) 与 mpmath
   高精度独立参考, 参考答案不由被测核心自身产生。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ResidualEvidence:
    absolute_fro: float
    relative_fro: float                     # / ||A||_F
    per_pair_relative: tuple[float, ...]   # ||Av-wv|| / max(|w|, scale*eps)
    max_per_pair_relative: float


@dataclass(frozen=True)
class OrthogonalityEvidence:
    deviation_fro: float                    # ||V^T V - I||_F
    relative_deviation: float               # / n
    max_abs_deviation: float


@dataclass(frozen=True)
class ReconstructionEvidence:
    absolute_fro: float
    relative_fro: float


def _scale(matrix: np.ndarray) -> float:
    return max(float(np.linalg.norm(matrix, ord="fro")),
               np.finfo(np.float64).eps)


def residual_evidence(
    matrix: np.ndarray, eigvals: np.ndarray, eigvecs: np.ndarray
) -> ResidualEvidence:
    scale = _scale(matrix)
    resid = matrix @ eigvecs - eigvecs * eigvals
    abs_fro = float(np.linalg.norm(resid, ord="fro"))
    per_pair = np.linalg.norm(resid, axis=0)
    denom = np.maximum(np.abs(eigvals), scale * np.finfo(np.float64).eps)
    per_pair_rel = tuple(float(x) for x in (per_pair / denom))
    return ResidualEvidence(
        absolute_fro=abs_fro,
        relative_fro=abs_fro / scale,
        per_pair_relative=per_pair_rel,
        max_per_pair_relative=max(per_pair_rel, default=0.0),
    )


def orthogonality_evidence(eigvecs: np.ndarray) -> OrthogonalityEvidence:
    n = eigvecs.shape[1]
    dev = eigvecs.T @ eigvecs - np.eye(n)
    fro = float(np.linalg.norm(dev, ord="fro"))
    return OrthogonalityEvidence(
        deviation_fro=fro,
        relative_deviation=fro / max(n, 1),
        max_abs_deviation=float(np.max(np.abs(dev))) if n else 0.0,
    )


def reconstruction_evidence(
    matrix: np.ndarray, eigvals: np.ndarray, eigvecs: np.ndarray
) -> ReconstructionEvidence:
    scale = _scale(matrix)
    reconstructed = (eigvecs * eigvals) @ eigvecs.T
    diff = reconstructed - matrix
    abs_fro = float(np.linalg.norm(diff, ord="fro"))
    return ReconstructionEvidence(
        absolute_fro=abs_fro, relative_fro=abs_fro / scale
    )


def cluster_eigenvalues(eigvals: np.ndarray, gap_tol: float) -> list[tuple[int, ...]]:
    """把升序特征值按间隙聚类。

    相邻间隙 ``w[i+1]-w[i] <= gap_tol * scale`` 归入同一簇,
    每簇对应一个 (近) 重特征子空间。scale 取特征值整体量级。
    """
    n = eigvals.shape[0]
    if n == 0:
        return []
    scale = max(float(np.max(np.abs(eigvals))), 1.0)
    gap = gap_tol * scale
    clusters: list[tuple[int, ...]] = []
    start = 0
    for i in range(n - 1):
        if eigvals[i + 1] - eigvals[i] > gap:
            clusters.append(tuple(range(start, i + 1)))
            start = i + 1
    clusters.append(tuple(range(start, n)))
    return clusters


def subspace_principal_angles(
    subspace_a: np.ndarray, subspace_b: np.ndarray
) -> np.ndarray:
    """两个等维子空间基之间的主角 (弧度)。

    用 ``sigma(Q_a^T Q_b)`` 的奇异值计算; 子空间重合 iff 全部奇异值为 1,
    最大偏差 ``max(1 - sigma_min)`` 即子空间距离度量。
    """
    qa, _ = np.linalg.qr(subspace_a)
    qb, _ = np.linalg.qr(subspace_b)
    singular = np.linalg.svd(qa.T @ qb, compute_uv=False)
    singular = np.clip(singular, 0.0, 1.0)
    return np.arccos(singular)


def compare_eigenbasis(
    eigvals_a: np.ndarray,
    eigvecs_a: np.ndarray,
    eigvals_b: np.ndarray,
    eigvecs_b: np.ndarray,
    gap_tol: float,
) -> dict:
    """对比两套特征分解: 特征值误差 + 重谱按子空间、单谱按向量。

    两套结果都必须已按升序排列。返回的每项都是具体数值证据。
    """
    n = eigvals_a.shape[0]
    scale = max(float(np.max(np.abs(eigvals_b))), 1.0)
    eigval_abs = np.abs(eigvals_a - eigvals_b)
    clusters = cluster_eigenvalues(eigvals_b, gap_tol)

    cluster_reports = []
    worst_angle = 0.0
    for group in clusters:
        cols_a = eigvecs_a[:, group]
        cols_b = eigvecs_b[:, group]
        if len(group) == 1:
            # 单特征值: 允许符号差, 比较 |内积|
            overlap = abs(float(cols_a[:, 0] @ cols_b[:, 0]))
            angle = float(np.arccos(np.clip(overlap, 0.0, 1.0)))
        else:
            angles = subspace_principal_angles(cols_a, cols_b)
            angle = float(np.max(angles))
        worst_angle = max(worst_angle, angle)
        cluster_reports.append({
            "indices": list(group),
            "multiplicity": len(group),
            "eigenvalue_min": float(eigvals_b[group[0]]),
            "eigenvalue_max": float(eigvals_b[group[-1]]),
            "max_principal_angle_rad": angle,
        })

    return {
        "n": n,
        "eigenvalue_max_abs_error": float(np.max(eigval_abs)) if n else 0.0,
        "eigenvalue_max_relative_error": float(
            np.max(eigval_abs / np.maximum(np.abs(eigvals_b), scale * 1e-300))
        ) if n else 0.0,
        "max_principal_angle_rad": worst_angle,
        "clusters": cluster_reports,
    }
