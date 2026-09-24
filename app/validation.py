"""输入校验：旋转合法性、6x6 协方差正定性、四元数等。

所有问题统一产出 ``Issue`` 字典，带 ``evidence_path``（最短证据路径），
不抛裸异常，供 API 层汇总为 422。
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .se3 import quat_to_rot

# 旋转合法 / 协方差正定的容差
ROT_ORTHO_TOL = 1e-6
ROT_DET_TOL = 1e-6
QUAT_NORM_TOL = 1e-6
COV_SYM_TOL = 1e-8
COV_PSD_TOL = 1e-10  # 最小特征值下限；严格 PD 需 > 0


def validate_rotation(rot: Any, where: str) -> tuple[np.ndarray | None, list[dict]]:
    """校验旋转输入（3x3 矩阵或四元数 [w,x,y,z]）。

    返回 (R 或 None, issues)。
    """
    issues: list[dict] = []
    if rot is None:
        issues.append(
            {
                "code": "MALFORMED_TRANSFORM",
                "message": f"{where}: rotation 缺失",
                "evidence_path": [where],
            }
        )
        return None, issues

    arr = np.asarray(rot, dtype=float)

    if arr.shape == (4,):
        n = float(np.linalg.norm(arr))
        if not np.isfinite(arr).all():
            issues.append(
                {
                    "code": "ILLEGAL_ROTATION",
                    "message": f"{where}: 四元数含非有限值",
                    "evidence_path": [where],
                }
            )
            return None, issues
        if abs(n - 1.0) > QUAT_NORM_TOL:
            issues.append(
                {
                    "code": "ILLEGAL_ROTATION",
                    "message": (
                        f"{where}: 四元数未归一化 |q|={n:.6g}，"
                        f"容差 {QUAT_NORM_TOL:g}"
                    ),
                    "evidence_path": [where],
                }
            )
            return None, issues
        R = quat_to_rot(arr)
        return R, issues

    if arr.shape != (3, 3):
        issues.append(
            {
                "code": "MALFORMED_TRANSFORM",
                "message": f"{where}: 旋转必须为 3x3 矩阵或长度 4 的四元数，实际 shape {arr.shape}",
                "evidence_path": [where],
            }
        )
        return None, issues

    if not np.isfinite(arr).all():
        issues.append(
            {
                "code": "ILLEGAL_ROTATION",
                "message": f"{where}: 旋转矩阵含非有限值",
                "evidence_path": [where],
            }
        )
        return None, issues

    ortho_err = float(np.linalg.norm(arr.T @ arr - np.eye(3), ord=np.inf))
    det = float(np.linalg.det(arr))
    if ortho_err > ROT_ORTHO_TOL:
        issues.append(
            {
                "code": "ILLEGAL_ROTATION",
                "message": (
                    f"{where}: 非正交 RᵀR-I 的 inf 范数={ortho_err:.3e}，"
                    f"容差 {ROT_ORTHO_TOL:g}"
                ),
                "evidence_path": [where],
                "details": {"orthogonality_error": ortho_err},
            }
        )
    if abs(det - 1.0) > ROT_DET_TOL:
        issues.append(
            {
                "code": "ILLEGAL_ROTATION",
                "message": (
                    f"{where}: det(R)={det:.6f} ≠ 1（反射或非旋转），"
                    f"容差 {ROT_DET_TOL:g}"
                ),
                "evidence_path": [where],
                "details": {"det": det},
            }
        )

    if issues:
        return None, issues
    # 数值重整到 SO(3)，消除输入上的微小舍入
    U, _, Vt = np.linalg.svd(arr)
    R = U @ Vt
    if np.linalg.det(R) < 0:  # 理论上 det==1 已校验，双保险
        R = U @ np.diag([1.0, 1.0, -1.0]) @ Vt
    return R, []


def validate_covariance(
    cov: Any, where: str, *, allow_missing: bool = True
) -> tuple[np.ndarray | None, list[dict]]:
    """校验 6x6 协方差矩阵：形状、对称、有限、半正定。

    协方差在物理上允许半正定（某个方向零不确定度）；传播与 Mahalanobis
    需要可逆时，调用方按 ``allow_singular`` 另行处理。缺失（None）合法，
    返回 (None, [])，由传播层标记 unknown。
    """
    if cov is None:
        if allow_missing:
            return None, []
        return None, [
            {
                "code": "MISSING_COVARIANCE",
                "message": f"{where}: 必须提供协方差矩阵",
                "evidence_path": [where],
            }
        ]

    arr = np.asarray(cov, dtype=float)
    if arr.shape != (6, 6):
        return None, [
            {
                "code": "MALFORMED_COVARIANCE",
                "message": f"{where}: 协方差必须为 6x6，实际 shape {arr.shape}",
                "evidence_path": [where],
            }
        ]
    if not np.isfinite(arr).all():
        return None, [
            {
                "code": "MALFORMED_COVARIANCE",
                "message": f"{where}: 协方差含非有限值",
                "evidence_path": [where],
            }
        ]

    sym_err = float(np.linalg.norm(arr - arr.T, ord=np.inf))
    if sym_err > COV_SYM_TOL:
        return None, [
            {
                "code": "COVARIANCE_NOT_SYMMETRIC",
                "message": (
                    f"{where}: 协方差不对称，|C-Cᵀ| inf 范数={sym_err:.3e}，"
                    f"容差 {COV_SYM_TOL:g}"
                ),
                "evidence_path": [where],
                "details": {"symmetry_error": sym_err},
            }
        ]

    arr = 0.5 * (arr + arr.T)  # 强制对称，消除舍入
    eigvals = np.linalg.eigvalsh(arr)
    eig_min = float(eigvals.min())
    if eig_min < -COV_PSD_TOL:
        # 给出最负方向作为“最短证据”：特征值 + 对应特征向量
        idx = int(np.argmin(eigvals))
        return None, [
            {
                "code": "COVARIANCE_NOT_PSD",
                "message": (
                    f"{where}: 协方差非半正定，最小特征值={eig_min:.6e} "
                    f"< -{COV_PSD_TOL:g}"
                ),
                "evidence_path": [where],
                "details": {
                    "eigenvalue_min": eig_min,
                    "worst_eigenvector": np.linalg.eigh(arr)[1][:, idx].tolist(),
                    "all_eigenvalues": eigvals.tolist(),
                },
            }
        ]

    # 微小负特征值钳为 0，保证后续 PSD 运算安全，并回告
    if eig_min < 0:
        eigvals_clipped = np.clip(eigvals, 0.0, None)
        arr = np.linalg.eigh(arr)[1] @ np.diag(eigvals_clipped) @ np.linalg.eigh(arr)[1].T
        arr = 0.5 * (arr + arr.T)
    return arr, []


def validate_cross_covariance(
    cross: Any, edge_a: str, edge_b: str
) -> tuple[np.ndarray | None, list[dict]]:
    """校验边 a、b 之间的 6x6 互协方差（不要求对称，但要求有限）。"""
    if cross is None:
        return None, []
    arr = np.asarray(cross, dtype=float)
    where = f"cross_covariance[{edge_a},{edge_b}]"
    if arr.shape != (6, 6):
        return None, [
            {
                "code": "MALFORMED_COVARIANCE",
                "message": f"{where}: 互协方差必须为 6x6，实际 shape {arr.shape}",
                "evidence_path": [f"edge:{edge_a}", f"edge:{edge_b}"],
            }
        ]
    if not np.isfinite(arr).all():
        return None, [
            {
                "code": "MALFORMED_COVARIANCE",
                "message": f"{where}: 互协方差含非有限值",
                "evidence_path": [f"edge:{edge_a}", f"edge:{edge_b}"],
            }
        ]
    return arr, []
