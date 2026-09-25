"""线性卡尔曼滤波核心：预测 + 缺测感知更新（Joseph 形式协方差更新）。

数值策略
--------
1. 协方差更新采用 Joseph 形式：
       P = (I - K H) P (I - K H)^T + K R K^T
   对舍入误差不敏感，能在病态情形下保持半正定性；随后再做对称化
   P <- (P + P^T)/2 消除非对称漂移。
2. 创新协方差 S = H P H^T + R 若病态（按 rcond 判定的有效秩亏），
   回退到 Moore-Penrose 伪逆，并在结果中标记 status="singular_innovation"。
3. 每步更新后检查 P 的最小特征值，若负值超出 psd_tol 判定为数值失败
   （KalmanNumericalError）；在容差内的微小负特征值裁剪到 0。

缺测约定
--------
update(z) 中 z 的某个分量为 None 或 NaN 视为该分量缺测；z 整体为 None
表示该步全部缺测（只做预测）。部分缺测时仅对观测到的分量子集做更新，
即取 H、R 的对应行（列）构成子系统。
"""

from dataclasses import dataclass, field

import numpy as np

from .exceptions import KalmanInputError, KalmanNumericalError
from .validation import (
    as_float_array,
    check_dim_limits,
    check_square,
    check_symmetric_psd,
)

# 每步状态的取值
STATUS_OK = "ok"                       # 正常更新
STATUS_PREDICT_ONLY = "predict_only"   # 该步全部缺测，仅预测
STATUS_SINGULAR = "singular_innovation"  # 创新协方差病态，使用伪逆回退


@dataclass(frozen=True)
class Tolerances:
    """数值容差配置。

    sym_tol:  对称性校验容差，max|M - M^T| 的上限。
    psd_tol:  半正定容差，特征值允许下探到 -psd_tol。
    rcond:    判定创新协方差病态的相对条件数阈值
              （特征值 <= rcond * 最大特征值 视为零方向）。
    """

    sym_tol: float = 1e-10
    psd_tol: float = 1e-12
    rcond: float = 1e-12

    def __post_init__(self):
        for name in ("sym_tol", "psd_tol", "rcond"):
            v = getattr(self, name)
            if not (isinstance(v, (int, float)) and np.isfinite(v) and v > 0):
                raise KalmanInputError(f"容差 {name} 必须为正有限数，实际为 {v!r}")


@dataclass
class StepResult:
    """单步滤波结果。"""

    step: int
    status: str
    x: np.ndarray
    P: np.ndarray
    missing: list = field(default_factory=list)   # 缺测分量下标
    n_observed: int = 0
    cond_S: float = 0.0                           # 创新协方差条件数（全缺测为 0）
    sym_err: float = 0.0                          # 对称化前的 max|P - P^T|
    min_eig_P: float = 0.0                        # 更新后 P 的最小特征值


class KalmanFilter:
    """定常线性模型的卡尔曼滤波器。

    模型：
        x_k = F x_{k-1} + w,  w ~ N(0, Q)
        z_k = H x_k + v,      v ~ N(0, R)
    F、Q、H、R 均为定常矩阵；Q、R 须对称半正定（R 允许奇异，例如零噪声
    通道，此时创新协方差可能病态，走伪逆回退路径）。
    """

    def __init__(self, F, Q, H, R, x0, P0, tolerances=None):
        self.tol = tolerances if tolerances is not None else Tolerances()

        F = as_float_array("F", F, 2)
        check_square("F", F)
        n = F.shape[0]
        H = as_float_array("H", H, 2)
        if H.shape[1] != n:
            raise KalmanInputError(
                f"H: 列数应等于状态维数 {n}，实际形状 {H.shape}"
            )
        m = H.shape[0]
        check_dim_limits(n, m)

        Q = as_float_array("Q", Q, 2)
        check_square("Q", Q, n)
        check_symmetric_psd("Q", Q, self.tol.sym_tol, self.tol.psd_tol)

        R = as_float_array("R", R, 2)
        check_square("R", R, m)
        check_symmetric_psd("R", R, self.tol.sym_tol, self.tol.psd_tol)

        x0 = as_float_array("x0", x0, 1)
        if x0.shape[0] != n:
            raise KalmanInputError(f"x0: 维数应为 {n}，实际为 {x0.shape[0]}")
        P0 = as_float_array("P0", P0, 2)
        check_square("P0", P0, n)
        check_symmetric_psd("P0", P0, self.tol.sym_tol, self.tol.psd_tol)

        self.F, self.Q, self.H, self.R = F, Q, H, R
        self.n, self.m = n, m
        self.x = x0.copy()
        self.P = 0.5 * (P0 + P0.T)
        self.step_count = 0

    # ------------------------------------------------------------------
    def predict(self):
        """时间更新：x <- F x, P <- F P F^T + Q（对称化）。"""
        self.x = self.F @ self.x
        P = self.F @ self.P @ self.F.T + self.Q
        self.P = 0.5 * (P + P.T)
        self.step_count += 1

    # ------------------------------------------------------------------
    def update(self, z):
        """量测更新。z 为 None 或含 None/NaN 分量表示缺测。返回 StepResult。"""
        observed = self._observed_indices(z)
        missing = [i for i in range(self.m) if i not in observed]

        if not observed:
            return StepResult(
                step=self.step_count,
                status=STATUS_PREDICT_ONLY,
                x=self.x.copy(),
                P=self.P.copy(),
                missing=missing,
                n_observed=0,
                min_eig_P=self._min_eig(self.P),
            )

        idx = np.array(observed, dtype=int)
        z_obs = np.array([z[i] for i in observed], dtype=float)
        H_o = self.H[idx, :]
        R_o = self.R[np.ix_(idx, idx)]

        y = z_obs - H_o @ self.x                    # 创新
        S = H_o @ self.P @ H_o.T + R_o              # 创新协方差
        S = 0.5 * (S + S.T)

        eig_S = np.linalg.eigvalsh(S)
        s_max = float(eig_S[-1])
        s_min = float(eig_S[0])
        cond_S = float("inf") if s_min <= 0.0 else s_max / max(s_min, 1e-300)

        PHt = self.P @ H_o.T
        if s_min <= self.tol.rcond * max(s_max, 1e-300):
            # 病态/奇异：伪逆回退
            K = PHt @ np.linalg.pinv(S, rcond=self.tol.rcond)
            status = STATUS_SINGULAR
        else:
            K = np.linalg.solve(S.T, H_o @ self.P).T   # K = P H^T S^{-1}
            status = STATUS_OK

        self.x = self.x + K @ y

        # Joseph 形式更新 + 对称化
        I_KH = np.eye(self.n) - K @ H_o
        P = I_KH @ self.P @ I_KH.T + K @ R_o @ K.T
        sym_err = float(np.max(np.abs(P - P.T))) if P.size else 0.0
        P = 0.5 * (P + P.T)

        min_eig = self._min_eig(P)
        if min_eig < -self.tol.psd_tol:
            raise KalmanNumericalError(
                f"第 {self.step_count} 步更新后协方差失去半正定性："
                f"最小特征值 {min_eig:.3e} 低于 -{self.tol.psd_tol:.1e}"
            )
        if min_eig < 0.0:
            # 容差内的微小负特征值：沿特征向量裁剪到 0
            w, V = np.linalg.eigh(P)
            P = (V * np.clip(w, 0.0, None)) @ V.T
            P = 0.5 * (P + P.T)
            min_eig = 0.0
        self.P = P

        if not (np.all(np.isfinite(self.x)) and np.all(np.isfinite(self.P))):
            raise KalmanNumericalError(
                f"第 {self.step_count} 步出现非有限值，滤波发散"
            )

        return StepResult(
            step=self.step_count,
            status=status,
            x=self.x.copy(),
            P=self.P.copy(),
            missing=missing,
            n_observed=len(observed),
            cond_S=cond_S,
            sym_err=sym_err,
            min_eig_P=min_eig,
        )

    # ------------------------------------------------------------------
    def step(self, z):
        """预测 + 更新，返回 StepResult。"""
        self.predict()
        return self.update(z)

    # ------------------------------------------------------------------
    def _observed_indices(self, z):
        """解析观测量，返回被观测到的分量下标列表。"""
        if z is None:
            return []
        if len(z) != self.m:
            raise KalmanInputError(
                f"z: 维数应为 {self.m}，实际为 {len(z)}"
            )
        observed = []
        for i, zi in enumerate(z):
            if zi is None:
                continue
            try:
                v = float(zi)
            except (TypeError, ValueError) as exc:
                raise KalmanInputError(f"z[{i}]: 无法解析为数值: {zi!r}") from exc
            if np.isnan(v):
                continue
            if not np.isfinite(v):
                raise KalmanInputError(f"z[{i}]: 必须为有限数或缺测(null/NaN)，实际为 {v}")
            observed.append(i)
        return observed

    @staticmethod
    def _min_eig(P):
        return float(np.linalg.eigvalsh(P)[0]) if P.size else 0.0
