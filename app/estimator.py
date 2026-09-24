"""陀螺零偏稳健估计。

观测模型（静止时真实角速度为 0，逐窗中位数近似消除单窗噪声）：

    g_meas(t) = b(t) + noise

三种候选模型（逐轴独立拟合）：
- ``constant``：b(t) = b0
- ``time_drift``：b(t) = b0 + k_t * (t - t_ref)          单位 rad/s/s
- ``temp_drift``：b(t) = b0 + k_T * (T - T_ref)          单位 rad/s/°C

估计方法：以每个静止窗样本数为权的加权最小二乘初值 + Huber IRLS（k=1.345），
异常窗（突然运动残留、峰值残留）被自动降权而不是直接删除；标准误用
三明治协方差（对异方差稳健），并与窗间稳健离散给出的 SE 取保守较大值。

不可观测性说明（重要，响应中也会显式给出）：
- 仅凭静止数据，加表零偏与重力矢量在载体系投影不可分（|a|≈g 只给一个标量约束）；
- 单一方位静止只能给出 b_a 在重力方向上的弱约束，水平方向加表零偏完全不可观测；
- 陀螺刻度因子、安装非正交、加速度敏感项（g-sensitivity）需要旋转激励，不可观测；
- 温漂与时漂在单调升温过程中共线，无法同时分离。
"""

from __future__ import annotations

import dataclasses

import numpy as np

HUBER_K = 1.345


@dataclasses.dataclass
class FitResult:
    model: str
    beta: np.ndarray           # (p,) 系数，beta[0] 始终是参考点零偏
    se: np.ndarray             # (p,) 三明治协方差标准误
    se_conservative: np.ndarray  # (p,) 与窗间稳健离散合并后的保守 SE
    resid: np.ndarray          # (m,) 加权残差
    sse: float                 # 加权 SSE
    weights: np.ndarray        # (m,) IRLS 最终权重
    scale: float               # 稳健残差尺度
    n: int
    t_ref: float
    temp_ref: float | None


def _huber_fit(
    X: np.ndarray,
    y: np.ndarray,
    w0: np.ndarray,
    t_ref: float,
    temp_ref: float | None,
    model: str,
    max_iter: int = 30,
    tol: float = 1e-9,
) -> FitResult:
    """单轴 Huber IRLS 加权回归。

    X: (m,p) 设计矩阵（首列为 1，即参考点处的零偏）；y: (m,)；w0: (m,) 样本权。
    """
    m = len(y)
    w = w0.astype(float).copy()
    beta = np.zeros(X.shape[1])
    for _ in range(max_iter):
        WX = X * w[:, None]
        gram = X.T @ WX
        gram += 1e-12 * np.eye(gram.shape[0]) * np.max(np.abs(gram))
        beta_new = np.linalg.solve(gram, X.T @ (w * y))
        if np.max(np.abs(beta_new - beta)) < tol:
            beta = beta_new
            break
        beta = beta_new
        r = y - X @ beta
        raw_s = 1.4826 * np.median(np.abs(r - np.median(r)))
        s = max(raw_s, 1e-12)
        u = np.abs(r) / (HUBER_K * s)
        huber_w = np.where(u <= 1.0, 1.0, 1.0 / np.maximum(u, 1e-12))
        w = w0 * huber_w

    r = y - X @ beta
    raw_s = 1.4826 * np.median(np.abs(r - np.median(r)))
    scale = max(raw_s, 1e-12)
    u = np.abs(r) / (HUBER_K * scale)
    huber_w = np.where(u <= 1.0, 1.0, 1.0 / np.maximum(u, 1e-12))
    w = w0 * huber_w

    # 三明治协方差：(X'WX)^-1 X' W' diag(psi^2 s^2) W X (X'WX)^-1
    WX = X * w[:, None]
    bread = np.linalg.inv(X.T @ WX + 1e-15 * np.eye(X.shape[1]))
    psi = np.where(u <= 1.0, r, HUBER_K * scale * np.sign(r))
    meat = X.T @ ((w0**2 * psi**2)[:, None] * X)
    cov = bread @ meat @ bread
    se = np.sqrt(np.maximum(np.diag(cov), 0.0))
    sse = float(np.sum(w * r**2))
    return FitResult(
        model=model,
        beta=beta,
        se=se,
        se_conservative=se.copy(),
        resid=r,
        sse=sse,
        weights=w,
        scale=scale,
        n=m,
        t_ref=t_ref,
        temp_ref=temp_ref,
    )


def _scatter_se(resid: np.ndarray, w: np.ndarray) -> float:
    """由窗间稳健离散估计常数项 SE：scale/sqrt(sum_effective_weights)。"""
    s = 1.4826 * np.median(np.abs(resid - np.median(resid)))
    s = max(s, 1e-12)
    return float(s / np.sqrt(max(np.sum(w), 1e-12)))


def fit_axis(
    t: np.ndarray,
    temp: np.ndarray | None,
    y: np.ndarray,
    counts: np.ndarray,
    cfg,
) -> tuple[dict, np.ndarray]:
    """对单轴做三种模型拟合并选择。

    返回 ``(JSON 可序列化字典片段, 选中模型的逐窗 IRLS 权重)``。
    """
    w0 = counts.astype(float)
    t_ref = float(np.median(t))
    temp_ref = float(np.median(temp)) if temp is not None else None
    dt = t - t_ref

    # --- constant ---
    X0 = np.ones((len(y), 1))
    c = _huber_fit(X0, y, w0, t_ref, temp_ref, "constant")
    c.se_conservative[0] = max(c.se[0], _scatter_se(c.resid, c.weights))

    chosen = c
    candidates = [_fit_to_dict(c, w0)]

    sse_threshold = c.sse * 0.80  # 至少降低 20% 加权 SSE 才接受复杂模型

    # --- time drift ---
    can_time = (
        len(y) >= cfg.drift_min_windows
        and float(np.max(t) - np.min(t)) >= cfg.drift_min_span
    )
    if can_time:
        Xt = np.column_stack([np.ones_like(dt), dt])
        ft = _huber_fit(Xt, y, w0, t_ref, temp_ref, "time_drift")
        ft.se_conservative[0] = max(
            ft.se[0], _scatter_se(ft.resid, ft.weights)
        )
        sig = abs(ft.beta[1]) > 1.96 * ft.se_conservative[1]
        if ft.sse < sse_threshold and sig:
            chosen = ft
        candidates.append(_fit_to_dict(ft, w0))

    # --- temperature drift ---
    can_temp = (
        temp is not None
        and len(y) >= cfg.temp_min_windows
        and float(np.max(temp) - np.min(temp)) >= cfg.temp_min_span
    )
    if can_temp:
        dT = temp - temp_ref
        XT = np.column_stack([np.ones_like(dT), dT])
        fT = _huber_fit(XT, y, w0, t_ref, temp_ref, "temp_drift")
        fT.se_conservative[0] = max(
            fT.se[0], _scatter_se(fT.resid, fT.weights)
        )
        sig = abs(fT.beta[1]) > 1.96 * fT.se_conservative[1]
        if fT.sse < sse_threshold and sig:
            # 温度与时间模型同时显著（共线）时优先温度模型，但标注无法分离
            chosen = fT
        candidates.append(_fit_to_dict(fT, w0))

    out = _fit_to_dict(chosen, w0)
    out["candidate_models"] = candidates
    out["time_drift_eligible"] = can_time
    out["temp_drift_eligible"] = can_temp
    return out, chosen.weights


def _fit_to_dict(f: FitResult, counts: np.ndarray | None = None) -> dict:
    slope = None
    slope_se = None
    if len(f.beta) >= 2:
        slope = float(f.beta[1])
        slope_se = float(f.se_conservative[1])
    norm_w = f.weights if counts is None else f.weights / np.maximum(counts, 1e-12)
    return {
        "model": f.model,
        "bias_at_reference": float(f.beta[0]),
        "bias_se": float(f.se_conservative[0]),
        "slope": slope,
        "slope_se": slope_se,
        "t_reference_s": f.t_ref,
        "temperature_reference_C": f.temp_ref,
        "robust_residual_scale": float(f.scale),
        "weighted_sse": f.sse,
        "n_windows": f.n,
        "min_window_weight": float(np.min(norm_w)),
        "median_window_weight": float(np.median(norm_w)),
    }
