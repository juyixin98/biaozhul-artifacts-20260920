"""线性卡尔曼模型定义与合法性校验。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import config
from .errors import DimensionError
from .validation import as_matrix, check_psd


@dataclass(frozen=True)
class LinearKalmanModel:
    """线性时不变状态空间模型。

    离散时间方程::

        x_k = F x_{k-1} + B u_{k-1} + w_{k-1},   w ~ N(0, Q)
        z_k = H x_k     + v_k,                   v ~ N(0, R)

    维度：状态 ``n``，量测 ``m``，控制输入 ``p``（无控制时 B=None）。

    参数在构造时一次性校验：维度自洽、Q/R 对称半正定、元素有限且在
    ``config.MAX_ABS_VALUE`` 范围内。
    """

    F: np.ndarray
    H: np.ndarray
    Q: np.ndarray
    R: np.ndarray
    B: np.ndarray | None = None

    def __post_init__(self) -> None:
        F = np.asarray(self.F, dtype=np.float64)
        H = np.asarray(self.H, dtype=np.float64)
        Q = np.asarray(self.Q, dtype=np.float64)
        R = np.asarray(self.R, dtype=np.float64)

        if F.ndim != 2 or F.shape[0] != F.shape[1]:
            raise DimensionError(f"F 必须是方阵，实际形状 {tuple(F.shape)}")
        n = F.shape[0]
        if not (1 <= n <= config.MAX_STATE_DIM):
            raise DimensionError(
                f"状态维度 n 必须在 [1, {config.MAX_STATE_DIM}]，实际 {n}"
            )
        if H.ndim != 2 or H.shape[1] != n:
            raise DimensionError(
                f"H 的形状必须为 (m, {n})（列数等于状态维），实际 {tuple(H.shape)}"
            )
        m = H.shape[0]
        if not (1 <= m <= config.MAX_MEAS_DIM):
            raise DimensionError(
                f"量测维度 m 必须在 [1, {config.MAX_MEAS_DIM}]，实际 {m}"
            )

        F = as_matrix("F", F, n, n)
        H = as_matrix("H", H, m, n)
        Q = as_matrix("Q", Q, n, n)
        R = as_matrix("R", R, m, m)
        check_psd("Q（过程噪声协方差）", Q)
        check_psd("R（量测噪声协方差）", R)

        B_arr = None
        if self.B is not None:
            B_in = np.asarray(self.B, dtype=np.float64)
            if B_in.ndim != 2 or B_in.shape[0] != n:
                raise DimensionError(
                    f"B 的形状必须为 (n={n}, p)，实际 {tuple(B_in.shape)}"
                )
            B_arr = as_matrix("B", B_in, n, B_in.shape[1])

        # frozen dataclass 通过 object.__setattr__ 写入校验后的数组
        object.__setattr__(self, "F", F)
        object.__setattr__(self, "H", H)
        object.__setattr__(self, "Q", Q)
        object.__setattr__(self, "R", R)
        object.__setattr__(self, "B", B_arr)

    @property
    def state_dim(self) -> int:
        return self.F.shape[0]

    @property
    def meas_dim(self) -> int:
        return self.H.shape[0]

    def validate_initial_state(self, x0: np.ndarray, P0: np.ndarray) -> None:
        n = self.state_dim
        if np.asarray(x0, dtype=np.float64).shape != (n,):
            raise DimensionError(f"x0 形状必须为 ({n},)，实际 {tuple(np.asarray(x0).shape)}")
        if np.asarray(P0, dtype=np.float64).shape != (n, n):
            raise DimensionError(
                f"P0 形状必须为 ({n}, {n})，实际 {tuple(np.asarray(P0).shape)}"
            )
