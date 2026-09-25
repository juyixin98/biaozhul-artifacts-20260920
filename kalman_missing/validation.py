"""输入校验：维度、有限性、对称性与半正定性。

规模上限（小中规模限定，可通过常量调整）：
- 状态维数 n <= MAX_STATE_DIM (64)
- 量测维数 m <= MAX_MEAS_DIM (64)
- 序列步数 <= MAX_STEPS (10000)
"""

import numpy as np

from .exceptions import KalmanInputError

MAX_STATE_DIM = 64
MAX_MEAS_DIM = 64
MAX_STEPS = 10000


def as_float_array(name, value, ndim):
    """把输入转成 float64 ndarray，并校验维数与有限性。"""
    try:
        arr = np.asarray(value, dtype=float)
    except (TypeError, ValueError) as exc:
        raise KalmanInputError(f"{name}: 无法解析为数值数组: {exc}") from exc
    if arr.ndim != ndim:
        raise KalmanInputError(
            f"{name}: 期望 {ndim} 维数组，实际为 {arr.ndim} 维（形状 {arr.shape}）"
        )
    if not np.all(np.isfinite(arr)):
        raise KalmanInputError(f"{name}: 含有 NaN 或 Inf，输入必须全部有限")
    return arr


def check_square(name, mat, size=None):
    if mat.shape[0] != mat.shape[1]:
        raise KalmanInputError(f"{name}: 必须为方阵，实际形状 {mat.shape}")
    if size is not None and mat.shape[0] != size:
        raise KalmanInputError(
            f"{name}: 维度应为 {size}x{size}，实际为 {mat.shape[0]}x{mat.shape[1]}"
        )


def check_symmetric(name, mat, sym_tol):
    """对称性校验：max|M - M^T| <= sym_tol。"""
    err = float(np.max(np.abs(mat - mat.T))) if mat.size else 0.0
    if err > sym_tol:
        raise KalmanInputError(
            f"{name}: 矩阵不对称，max|M-M^T|={err:.3e} 超过容差 {sym_tol:.1e}"
        )
    return err


def check_psd(name, mat, psd_tol):
    """半正定校验：最小特征值 >= -psd_tol。返回最小特征值。"""
    min_eig = float(np.linalg.eigvalsh(mat)[0]) if mat.size else 0.0
    if min_eig < -psd_tol:
        raise KalmanInputError(
            f"{name}: 非半正定，最小特征值 {min_eig:.3e} 低于 -{psd_tol:.1e}"
        )
    return min_eig


def check_symmetric_psd(name, mat, sym_tol, psd_tol):
    check_symmetric(name, mat, sym_tol)
    return check_psd(name, mat, psd_tol)


def check_dim_limits(n, m):
    if not (1 <= n <= MAX_STATE_DIM):
        raise KalmanInputError(
            f"状态维数 n={n} 超出允许范围 [1, {MAX_STATE_DIM}]（本库限定小中规模）"
        )
    if not (1 <= m <= MAX_MEAS_DIM):
        raise KalmanInputError(
            f"量测维数 m={m} 超出允许范围 [1, {MAX_MEAS_DIM}]（本库限定小中规模）"
        )


def check_num_steps(k):
    if not (0 <= k <= MAX_STEPS):
        raise KalmanInputError(
            f"步数 {k} 超出允许范围 [0, {MAX_STEPS}]（本库限定小中规模）"
        )
