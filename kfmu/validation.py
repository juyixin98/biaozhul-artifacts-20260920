"""输入合法性校验。"""

from __future__ import annotations

import numpy as np

from . import config
from .errors import DimensionError, InvalidValueError, MatrixPropertyError


def as_vector(name: str, value, dim: int) -> np.ndarray:
    """把输入转成长度为 ``dim`` 的一维 float64 向量并做范围检查。"""
    try:
        arr = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise InvalidValueError(f"{name} 必须是数值数组") from exc
    if arr.ndim != 1 or arr.shape[0] != dim:
        raise DimensionError(
            f"{name} 的形状必须为 ({dim},)，实际为 {tuple(arr.shape)}"
        )
    _check_finite(name, arr)
    return arr


def as_matrix(name: str, value, rows: int, cols: int) -> np.ndarray:
    """把输入转成 ``rows x cols`` 的 float64 矩阵并做范围检查。"""
    try:
        arr = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise InvalidValueError(f"{name} 必须是数值矩阵") from exc
    if arr.ndim != 2 or arr.shape != (rows, cols):
        raise DimensionError(
            f"{name} 的形状必须为 ({rows}, {cols})，实际为 {tuple(arr.shape)}"
        )
    _check_finite(name, arr)
    return arr


def _check_finite(name: str, arr: np.ndarray) -> None:
    if not np.all(np.isfinite(arr)):
        raise InvalidValueError(f"{name} 含有 NaN 或无穷大")
    if arr.size and float(np.max(np.abs(arr))) > config.MAX_ABS_VALUE:
        raise InvalidValueError(
            f"{name} 含有绝对值超过 {config.MAX_ABS_VALUE:g} 的元素"
        )


def check_symmetric(name: str, mat: np.ndarray) -> None:
    """要求方阵在容差内对称。"""
    if mat.ndim != 2 or mat.shape[0] != mat.shape[1]:
        raise DimensionError(f"{name} 必须是方阵，实际形状 {tuple(mat.shape)}")
    asym = float(np.max(np.abs(mat - mat.T))) if mat.size else 0.0
    scale = float(np.max(np.abs(mat))) if mat.size else 0.0
    tol = config.SYMMETRY_ATOL + config.SYMMETRY_RTOL * max(scale, 1.0)
    if asym > tol:
        raise MatrixPropertyError(
            f"{name} 不对称：最大偏差 {asym:.3g} 超过容差 {tol:.3g}"
        )


def check_psd(name: str, mat: np.ndarray) -> None:
    """要求对称矩阵在容差内半正定（最小特征值不能显著为负）。"""
    check_symmetric(name, mat)
    eigvals = np.linalg.eigvalsh(mat)
    eig_min = float(eigvals[0])
    eig_max = float(eigvals[-1])
    tol = config.PSD_FLOOR + config.PSD_RTOL * max(eig_max, 1.0)
    if eig_min < -tol:
        raise MatrixPropertyError(
            f"{name} 不是半正定：最小特征值 {eig_min:.6g} 小于容差 {-tol:.6g}"
        )


def check_positive_definite(name: str, mat: np.ndarray) -> None:
    """要求对称矩阵正定（Cholesky 可分解）。"""
    check_symmetric(name, mat)
    try:
        np.linalg.cholesky(mat)
    except np.linalg.LinAlgError as exc:
        raise MatrixPropertyError(f"{name} 不是正定矩阵（Cholesky 分解失败）") from exc
