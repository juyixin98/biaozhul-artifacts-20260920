"""线性卡尔曼滤波：预测步 + 支持部分观测缺失的更新步。

数值稳定性策略
--------------
1. 创新协方差 ``S = H P H' + R`` 在更新前先对称化并做 Cholesky 分解；
   只用 Cholesky 因子通过前/后代入求解 ``S^{-1} e`` 与 ``S^{-1} H``，
   不构造矩阵的逆。
2. 协方差后验用 **Joseph 形式** ``P+ = (I-KH) P (I-KH)' + K R K'`` 计算，
   它对增益误差不敏感，且在 R 半正定时仍保持对称半正定结构；最后再做
   一次对称化。
3. 每步检查 ``P`` 的对称性与半正定（容差见 ``kfmu.config``），超出容差
   抛出 :class:`NumericalStabilityError`，而不是悄悄返回错误结果。

缺测约定
--------
* 量测向量中分量为 ``NaN`` 或掩码 ``available[i] == False`` 表示该分量缺测。
* 缺测分量从 ``H/R/z`` 中按行/列删除后，在**观测子空间**内做标准更新；
  全部缺测时退化为纯预测（``status = "predicted_only"``）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import config
from .errors import DimensionError, InvalidValueError
from .linalg import safe_cholesky, solve_spd_via_cholesky
from .model import LinearKalmanModel
from .validation import _check_finite, as_vector, check_psd, check_symmetric


@dataclass
class StepResult:
    """单步结果。

    ``status`` 取值：

    * ``"updated"``：至少一个量测分量参与了更新；
    * ``"predicted_only"``：本步全部量测缺测，只做了时间更新。
    """

    x: np.ndarray
    P: np.ndarray
    status: str
    available: np.ndarray  # bool[m]，本步实际使用的量测分量
    innovation: np.ndarray | None  # 观测子空间内的创新；全缺测时为 None
    S: np.ndarray | None  # 观测子空间内的创新协方差；全缺测时为 None
    K: np.ndarray | None  # n x m_obs 卡尔曼增益；全缺测时为 None


class KalmanFilter:
    """带状态的线性卡尔曼滤波器。

    典型用法::

        kf = KalmanFilter(model, x0, P0)
        r = kf.predict()
        r = kf.update(z_with_nans_or_mask)

    也可直接用 :func:`predict` / :func:`update` 的无状态函数式接口。
    """

    def __init__(self, model: LinearKalmanModel, x0, P0) -> None:
        self.model = model
        n = model.state_dim
        x0 = np.asarray(x0, dtype=np.float64)
        P0 = np.asarray(P0, dtype=np.float64)
        model.validate_initial_state(x0, P0)
        _check_finite("x0", x0)
        # P0 只要求对称 + PSD 容差
        check_symmetric("P0（初始协方差）", P0)
        check_psd("P0（初始协方差）", P0)
        self.x = x0.copy()
        self.P = 0.5 * (P0 + P0.T)

    # ------------------------------------------------------------------ #
    def predict(self, u=None) -> StepResult:
        """时间更新（先验）。可选控制输入 ``u``（需模型提供 B）。"""
        self.x, self.P = predict(self.model, self.x, self.P, u=u)
        return StepResult(
            x=self.x.copy(),
            P=self.P.copy(),
            status="predicted_only",
            available=np.zeros(self.model.meas_dim, dtype=bool),
            innovation=None,
            S=None,
            K=None,
        )

    def update(self, z, available=None) -> StepResult:
        """量测更新，支持部分分量缺失。

        ``z``：长度为 ``m`` 的量测向量，缺测分量可填 ``NaN``；
        ``available``：可选布尔掩码，长度为 ``m``，False 表示缺测。
        NaN 与掩码取交集（任一判定缺测即为缺测）。
        """
        avail = _parse_available(z, available, self.model.meas_dim)
        if not np.any(avail):
            # 全部缺测：不做量测更新，状态保持为先验
            return StepResult(
                x=self.x.copy(),
                P=self.P.copy(),
                status="predicted_only",
                available=avail,
                innovation=None,
                S=None,
                K=None,
            )

        self.x, self.P, innovation, S, K = _update_subspace(
            self.model, self.x, self.P, z, avail
        )
        return StepResult(
            x=self.x.copy(),
            P=self.P.copy(),
            status="updated",
            available=avail,
            innovation=innovation,
            S=S,
            K=K,
        )

    def step(self, z=None, available=None, u=None) -> StepResult:
        """便捷方法：一次 predict + update。

        ``z=None`` 或全部分量缺测时等价于纯预测。
        """
        self.predict(u=u)
        if z is None:
            return StepResult(
                x=self.x.copy(),
                P=self.P.copy(),
                status="predicted_only",
                available=np.zeros(self.model.meas_dim, dtype=bool),
                innovation=None,
                S=None,
                K=None,
            )
        return self.update(z, available)


# ---------------------------------------------------------------------- #
# 无状态函数式接口
# ---------------------------------------------------------------------- #
def predict(model: LinearKalmanModel, x: np.ndarray, P: np.ndarray, u=None):
    """返回先验 ``(x_pred, P_pred)``。"""
    n = model.state_dim
    x = as_vector("x", x, n)
    P = np.asarray(P, dtype=np.float64)
    if P.shape != (n, n):
        raise DimensionError(f"P 形状必须为 ({n}, {n})，实际 {tuple(P.shape)}")
    check_symmetric("P", P)
    check_psd("P", P)

    x_pred = model.F @ x
    if u is not None:
        if model.B is None:
            raise DimensionError("模型没有控制矩阵 B，但提供了控制输入 u")
        u_arr = np.asarray(u, dtype=np.float64)
        if u_arr.ndim != 1 or u_arr.shape[0] != model.B.shape[1]:
            raise DimensionError(
                f"u 形状必须为 ({model.B.shape[1]},)，实际 {tuple(u_arr.shape)}"
            )
        _check_finite("u", u_arr)
        x_pred = x_pred + model.B @ u_arr

    P_pred = model.F @ P @ model.F.T + model.Q
    _guarantee_covariance(P_pred, "先验协方差 P_pred")
    return x_pred, P_pred


def update(model: LinearKalmanModel, x: np.ndarray, P: np.ndarray, z, available=None):
    """无状态量测更新。返回 :class:`StepResult`，不修改输入。"""
    n, m = model.state_dim, model.meas_dim
    x = as_vector("x", x, n)
    P = np.asarray(P, dtype=np.float64)
    if P.shape != (n, n):
        raise DimensionError(f"P 形状必须为 ({n}, {n})，实际 {tuple(P.shape)}")
    check_symmetric("P（先验协方差）", P)
    check_psd("P（先验协方差）", P)
    avail = _parse_available(z, available, m)

    if not np.any(avail):
        return StepResult(
            x=x.copy(),
            P=P.copy(),
            status="predicted_only",
            available=avail,
            innovation=None,
            S=None,
            K=None,
        )

    x_up, P_up, innovation, S, K = _update_subspace(model, x, P, z, avail)
    return StepResult(
        x=x_up,
        P=P_up,
        status="updated",
        available=avail,
        innovation=innovation,
        S=S,
        K=K,
    )


# ---------------------------------------------------------------------- #
# 内部实现
# ---------------------------------------------------------------------- #
def _parse_available(z, available, m: int) -> np.ndarray:
    """合并 NaN 与布尔掩码，返回长度 m 的布尔数组。"""
    z_arr = np.asarray(z, dtype=np.float64)
    if z_arr.shape != (m,):
        raise DimensionError(f"z 形状必须为 ({m},)，实际 {tuple(z_arr.shape)}")
    # NaN 分量视为缺测；其余分量必须有限（inf 不允许）
    finite = np.isfinite(z_arr)
    if np.any(np.isinf(z_arr)):
        raise InvalidValueError("z 含有无穷大（缺测请用 null/NaN）")

    if available is None:
        avail = finite
    else:
        avail_arr = np.asarray(available)
        if avail_arr.shape != (m,):
            raise DimensionError(
                f"available 掩码形状必须为 ({m},)，实际 {tuple(avail_arr.shape)}"
            )
        if avail_arr.dtype != bool:
            raise InvalidValueError("available 必须是布尔数组")
        avail = finite & avail_arr
    return avail


def _update_subspace(model: LinearKalmanModel, x, P, z, available):
    """在观测子空间内执行标准卡尔曼更新。"""
    H_obs = model.H[available, :]
    R_obs = model.R[np.ix_(available, available)]
    z_obs = np.asarray(z, dtype=np.float64)[available]
    _check_finite("z（有效分量）", z_obs)

    # 创新协方差：对称化后 Cholesky
    S = H_obs @ P @ H_obs.T + R_obs
    S = 0.5 * (S + S.T)
    L = safe_cholesky(S, name="创新协方差 S")

    innovation = z_obs - H_obs @ x

    # K = P H' S^{-1}：通过 Cholesky 因子解 S X = I，不显式求逆
    sinv = solve_spd_via_cholesky(L, np.eye(S.shape[0]))
    K = P @ H_obs.T @ sinv

    x_up = x + K @ innovation

    # Joseph 形式：在 R_obs 半正定时也保持对称半正定结构
    n = P.shape[0]
    IKH = np.eye(n) - K @ H_obs
    P_up = IKH @ P @ IKH.T + K @ R_obs @ K.T
    _guarantee_covariance(P_up, "后验协方差 P_post")
    return x_up, P_up, innovation, S, K


def _guarantee_covariance(mat: np.ndarray, name: str) -> np.ndarray:
    """协方差后处理：对称检查 + PSD 容差检查；就地对称化。

    最小特征值在 ``PSD_FLOOR + PSD_RTOL * eig_max`` 内视为数值半正定，
    就地写回对称化结果；显著负定时抛错。
    """
    sym = 0.5 * (mat + mat.T)
    asym = float(np.max(np.abs(mat - mat.T))) if mat.size else 0.0
    scale = float(np.max(np.abs(mat))) if mat.size else 0.0
    sym_tol = config.COV_SYM_FLOOR + config.SYMMETRY_RTOL * max(scale, 1.0)
    if asym > sym_tol:
        from .errors import NumericalStabilityError

        raise NumericalStabilityError(
            f"{name} 失去对称性：最大偏差 {asym:.3g} 超过容差 {sym_tol:.3g}",
            code="covariance_asymmetry",
        )

    eigvals = np.linalg.eigvalsh(sym)
    eig_min = float(eigvals[0])
    eig_max = float(eigvals[-1])
    psd_tol = config.PSD_FLOOR + config.PSD_RTOL * max(eig_max, 1.0)
    if eig_min < -psd_tol:
        from .errors import NumericalStabilityError

        raise NumericalStabilityError(
            f"{name} 不是半正定：最小特征值 {eig_min:.6g} 小于容差 {-psd_tol:.6g}",
            code="covariance_not_psd",
        )

    # 容差内的对称化就地写回（mat 来自调用方的新数组，直接改其内容）
    mat[...] = sym
    return mat
