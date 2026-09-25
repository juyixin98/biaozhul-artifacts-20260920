"""Huber 损失的迭代重加权最小二乘（IRLS / MM 迭代）。

目标函数（n 个样本，p 个特征）：

    Q(β, b) = Σ_i ρδ(y_i - x_iᵀβ - b) + (λ/2) ‖β‖²

其中
    ρδ(r) = 0.5 r²                  若 |r| <= δ
            δ |r| - 0.5 δ²         若 |r| >  δ

截距 b 不参与惩罚。IRLS 在第 k 轮令权重
    w_i = 1              若 |r_i^(k)| <= δ
          δ / |r_i^(k)|  否则
并求解加权岭回归得到下一组系数。这是 Huber 损失的 MM 迭代：
加权二次代理函数在当前点与 Huber 损失相切且处处在其上方，因此
完整步的目标值单调不增（实现中另带回溯减半作为数值安全兜底）。

δ="auto"：以 OLS 初值残差的 MAD 尺度估计 δ = 1.4826·MAD
（MAD = median|r - median(r)|）；残差无散布时按文档化的回退规则处理。

失败状态（status）：
- "converged"：在 tol 下收敛
- "max_iterations_reached"：达到 max_iter 仍未收敛（结果仍返回）
- "objective_non_decreasing"：回溯 50 次仍找不到下降步（罕见数值病态）
数值上完全无法继续时抛出 NumericalError，由 API 层映射为
status="numerical_error"。
"""

from __future__ import annotations

from typing import Any, Union

import numpy as np

from .linalg import solve_ols, solve_weighted_ridge
from .validation import (
    DELTA_MIN,
    NumericalError,
    validate_huber_params,
    validate_xy,
)

# 权重下界，防止除零并限制单个极端离群点的“零杠杆”
WEIGHT_FLOOR = 1e-12
# 下降判定的数值容差（允许 SVD 舍入级别的上升）
MONOTONIC_SLACK = 1e-12
# 回溯减半最大次数
MAX_LINE_SEARCH_STEPS = 50

WARN_RANK_DEFICIENT = "rank_deficient_min_norm_solution"
WARN_AUTO_MAD = "auto_scale_from_mad"
WARN_ZERO_RESIDUAL = "auto_scale_all_zero_residuals"
WARN_ZERO_SPREAD = "auto_scale_zero_spread_fallback_residual_std"


def huber_weights(residuals: np.ndarray, delta: float) -> np.ndarray:
    """IRLS 权重：内部点 1，外部点 δ/|r|。"""
    abs_r = np.abs(residuals)
    w = np.ones_like(abs_r)
    outside = abs_r > delta
    w[outside] = delta / abs_r[outside]
    return np.clip(w, WEIGHT_FLOOR, 1.0)


def huber_objective(
    X: np.ndarray,
    y: np.ndarray,
    coef: np.ndarray,
    intercept: float,
    delta: float,
    reg_lambda: float = 0.0,
) -> float:
    """Huber 目标值：Σ ρδ(r) + (λ/2)‖coef‖²。"""
    r = y - X @ coef - intercept
    abs_r = np.abs(r)
    quad = 0.5 * r * r
    lin = delta * abs_r - 0.5 * delta * delta
    rho = np.where(abs_r <= delta, quad, lin)
    return float(np.sum(rho) + 0.5 * reg_lambda * float(coef @ coef))


def fit_ols(X: np.ndarray, y: np.ndarray, fit_intercept: bool = True) -> dict:
    """普通最小二乘对照（SVD 最小范数解，秩亏安全）。"""
    X_arr, y_arr = validate_xy(X, y)
    sol = solve_ols(X_arr, y_arr, fit_intercept=fit_intercept)
    residual = y_arr - X_arr @ sol["coef"] - sol["intercept"]
    return {
        "coefficients": [float(v) for v in sol["coef"]],
        "intercept": float(sol["intercept"]),
        "objective_sse": float(residual @ residual),
        "rank": sol["rank"],
        "rank_deficient": sol["rank_deficient"],
        "fit_intercept": fit_intercept,
    }


def _resolve_auto_delta(
    residuals: np.ndarray,
    y_scale: float,
) -> tuple[float, str, float]:
    """按 MAD 规则解析 δ="auto"。

    返回 (delta, warning_code, scale)。

    MAD 为零或低于零散布容差（1e-12·max(1, |y|)，用于吸收 SVD
    舍入噪声）时，按 OLS 残差标准差 / 统一常数回退。
    """
    med = float(np.median(residuals))
    mad = float(np.median(np.abs(residuals - med)))
    scale_tol = 1e-12 * max(1.0, y_scale)
    if mad > scale_tol:
        scale = 1.4826 * mad
        return scale, WARN_AUTO_MAD, scale
    # MAD ≈ 0：残差没有可分辨的绝对偏差散布
    max_abs = float(np.max(np.abs(residuals)))
    if max_abs <= scale_tol:
        # 完美拟合（全零残差）；δ 取何值已不影响结果
        return 1.0, WARN_ZERO_RESIDUAL, 0.0
    # 残差为同一非零常数（如不拟合截距时整体偏移）：
    # 回退到 OLS 残差尺度 1.349σ
    sigma = float(np.std(residuals))
    if sigma > scale_tol:
        scale = 1.349 * sigma
        return scale, WARN_ZERO_SPREAD, scale
    return max(1.0, max_abs), WARN_ZERO_SPREAD, max(1.0, max_abs)


def _split_beta(beta: np.ndarray, p: int, fit_intercept: bool):
    if fit_intercept:
        return beta[:p], float(beta[p])
    return beta, 0.0


def fit_huber(
    X: Any,
    y: Any,
    *,
    delta: Union[str, float] = "auto",
    reg_lambda: float = 0.0,
    tol: float = 1e-7,
    max_iter: int = 100,
    fit_intercept: bool = True,
) -> dict:
    """Huber IRLS 拟合。参数范围见 module docstring 与 validation.py。"""
    X_arr, y_arr = validate_xy(X, y)
    n, p = X_arr.shape

    auto_delta = delta == "auto"
    if not auto_delta:
        delta_f, reg_f, tol_f, max_iter_i, fit_intercept = (
            validate_huber_params(
                delta, reg_lambda, tol, max_iter, fit_intercept
            )
        )
    else:
        _, reg_f, tol_f, max_iter_i, fit_intercept = validate_huber_params(
            DELTA_MIN, reg_lambda, tol, max_iter, fit_intercept
        )
        delta_f = None

    warnings_list: list[str] = []

    # 初值：OLS（SVD 伪逆，秩亏时最小范数解）
    ols = solve_ols(X_arr, y_arr, fit_intercept=fit_intercept)
    beta = ols["beta"].copy()
    rank = ols["rank"]
    if ols["rank_deficient"]:
        warnings_list.append(WARN_RANK_DEFICIENT)

    coef, intercept = _split_beta(beta, p, fit_intercept)
    residual = y_arr - X_arr @ coef - intercept

    scale_used = None
    if auto_delta:
        y_scale = float(np.max(np.abs(y_arr)))
        delta_f, warn_code, scale_used = _resolve_auto_delta(
            residual, y_scale
        )
        warnings_list.append(warn_code)

    objective = huber_objective(
        X_arr, y_arr, coef, intercept, delta_f, reg_f
    )
    objective_history = [objective]

    status = "max_iterations_reached"
    iterations = 0

    for iteration in range(1, max_iter_i + 1):
        iterations = iteration
        weights = huber_weights(residual, delta_f)
        wls = solve_weighted_ridge(
            X_arr,
            y_arr,
            weights,
            reg_f,
            fit_intercept=fit_intercept,
        )
        beta_new = wls["beta"]
        if not np.all(np.isfinite(beta_new)):
            raise NumericalError("加权最小二乘解含 NaN/Inf，数值计算失败")

        coef_new, intercept_new = _split_beta(
            beta_new, p, fit_intercept
        )
        obj_new = huber_objective(
            X_arr, y_arr, coef_new, intercept_new, delta_f, reg_f
        )

        # 回溯线搜索（MM 理论保证完整步不增；此处仅防御舍入误差）
        accepted = False
        step = 1.0
        best_beta = beta_new
        best_obj = obj_new
        threshold = objective + MONOTONIC_SLACK * (1.0 + abs(objective))
        for _ in range(MAX_LINE_SEARCH_STEPS):
            cand_beta = beta + step * (beta_new - beta)
            cand_coef, cand_intercept = _split_beta(
                cand_beta, p, fit_intercept
            )
            cand_obj = huber_objective(
                X_arr,
                y_arr,
                cand_coef,
                cand_intercept,
                delta_f,
                reg_f,
            )
            if cand_obj < best_obj:
                best_beta, best_obj = cand_beta, cand_obj
            if cand_obj <= threshold:
                beta_new, obj_new = cand_beta, cand_obj
                accepted = True
                break
            step *= 0.5

        if not accepted:
            beta_new, obj_new = best_beta, best_obj
            status = "objective_non_decreasing"

        beta_diff = float(np.linalg.norm(beta_new - beta))
        beta_scale = max(1.0, float(np.linalg.norm(beta)))
        obj_rel = abs(obj_new - objective) / max(1.0, abs(objective))
        beta_rel = beta_diff / beta_scale

        beta = beta_new
        objective = obj_new
        objective_history.append(objective)
        coef, intercept = _split_beta(beta, p, fit_intercept)
        residual = y_arr - X_arr @ coef - intercept

        if beta_rel <= tol_f and obj_rel <= tol_f:
            status = "converged"
            break
        if not accepted:
            break

    return {
        "coefficients": [float(v) for v in coef],
        "intercept": float(intercept),
        "delta_used": float(delta_f),
        "residual_scale": (None if scale_used is None else float(scale_used)),
        "reg_lambda": reg_f,
        "tol": tol_f,
        "max_iter": max_iter_i,
        "fit_intercept": fit_intercept,
        "iterations": iterations,
        "converged": status == "converged",
        "status": status,
        "objective": float(objective),
        "objective_history": [float(v) for v in objective_history],
        "rank": int(rank),
        "rank_deficient": bool(rank < (p + (1 if fit_intercept else 0))),
        "warnings": warnings_list,
    }
