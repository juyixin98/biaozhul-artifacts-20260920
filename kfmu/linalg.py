"""数值稳定的小型线性代数工具。

更新步骤只用 Cholesky 分解 + 前/后代入求解，不构造矩阵的逆。
"""

from __future__ import annotations

import numpy as np

from .errors import NumericalStabilityError


def safe_cholesky(mat: np.ndarray, name: str = "矩阵") -> np.ndarray:
    """对称正定矩阵的 Cholesky 分解，失败时抛出带状态码的异常。

    返回下三角矩阵 ``L``，满足 ``L @ L.T == mat``。
    """
    try:
        return np.linalg.cholesky(mat)
    except np.linalg.LinAlgError as exc:
        raise NumericalStabilityError(
            f"{name} 的 Cholesky 分解失败：矩阵在数值上奇异或不正定",
            code="singular_innovation_covariance"
            if name == "创新协方差 S"
            else "cholesky_failed",
        ) from exc


def solve_spd_via_cholesky(lower: np.ndarray, rhs: np.ndarray) -> np.ndarray:
    """求解 ``(L L.T) X = rhs``，``lower`` 为 Cholesky 下三角因子。

    ``rhs`` 可以是向量或矩阵（右端按列）。
    """
    y = np.linalg.solve(lower, rhs)  # L y = rhs
    return np.linalg.solve(lower.T, y)  # L.T x = y


def symmetrize(mat: np.ndarray, floor: float = 0.0) -> np.ndarray:
    """对称化并截断不显著的负特征值，返回保证 PSD 的对称矩阵。

    仅用于"计算结果在 PSD 容差内"之后的数值清理；调用方需先确认
    最小特征值没有显著小于零。
    """
    sym = 0.5 * (mat + mat.T)
    eigvals, eigvecs = np.linalg.eigh(sym)
    cutoff = floor
    if float(eigvals[-1]) > 0.0:
        cutoff = max(floor, 1e-14 * abs(float(eigvals[-1])))
    clipped = np.clip(eigvals, cutoff, None)
    return (eigvecs * clipped) @ eigvecs.T
