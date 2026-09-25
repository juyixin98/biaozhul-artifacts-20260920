"""输入校验：明确输入范围、数值容差与失败状态。

规模限定（小中规模，可在常量处调整）：
- 1 <= n <= 100_000
- 1 <= p <= 200
- 所有输入元素有限（非 NaN / Inf）
- |x_ij|, |y_i| <= 1e8
- delta      in [1e-12, 1e12]
- reg_lambda in [0.0, 1e12]
- tol        in [1e-14, 1.0]
- max_iter   in [1, 10_000]
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

MAX_SAMPLES = 100_000
MAX_FEATURES = 200
MAX_ABS_VALUE = 1e8

DELTA_MIN = 1e-12
DELTA_MAX = 1e12
LAMBDA_MAX = 1e12
TOL_MIN = 1e-14
TOL_MAX = 1.0
MAX_ITER_MIN = 1
MAX_ITER_UPPER = 10_000
RCOND = 1e-10


class InputError(ValueError):
    """请求输入不合法（对应 JSON 接口 status="input_error"）。"""


class NumericalError(RuntimeError):
    """输入合法但数值计算失败（对应 status="numerical_error"）。"""


def _as_float_vector(value: Any, name: str) -> np.ndarray:
    try:
        arr = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise InputError(f"{name} 必须是数值数组，无法转换为 float64：{exc}")
    if arr.ndim != 1:
        raise InputError(f"{name} 必须是一维数组，实际维度 {arr.ndim}")
    if arr.size == 0:
        raise InputError(f"{name} 不能为空")
    _check_finite_and_range(arr, name)
    return arr


def _as_float_matrix(value: Any, name: str) -> np.ndarray:
    try:
        arr = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise InputError(f"{name} 必须是数值数组，无法转换为 float64：{exc}")
    if arr.ndim != 2:
        raise InputError(f"{name} 必须是二维数组，实际维度 {arr.ndim}")
    if arr.shape[0] == 0 or arr.shape[1] == 0:
        raise InputError(f"{name} 不能为空")
    _check_finite_and_range(arr, name)
    return arr


def _check_finite_and_range(arr: np.ndarray, name: str) -> None:
    if not np.all(np.isfinite(arr)):
        raise InputError(f"{name} 含 NaN 或无穷大，只接受有限数值")
    abs_max = float(np.max(np.abs(arr)))  # 此时已全部有限
    if abs_max > MAX_ABS_VALUE:
        raise InputError(
            f"{name} 存在绝对值超过 {MAX_ABS_VALUE:g} 的元素；"
            "请先对数据做缩放"
        )


def _check_scalar_range(value: float, lo: float, hi: float, name: str) -> None:
    if not math.isfinite(value):
        raise InputError(f"{name} 必须是有限数值")
    if not (lo <= value <= hi):
        raise InputError(f"{name}={value:g} 超出允许范围 [{lo:g}, {hi:g}]")


def validate_xy(X: Any, y: Any) -> tuple[np.ndarray, np.ndarray]:
    """校验并返回 float64 的 X (n,p) 与 y (n,)。"""
    X_arr = _as_float_matrix(X, "X")
    y_arr = _as_float_vector(y, "y")
    n, p = X_arr.shape
    if n > MAX_SAMPLES or p > MAX_FEATURES:
        raise InputError(
            f"数据规模 (n={n}, p={p}) 超出小中规模限定 "
            f"(n<={MAX_SAMPLES}, p<={MAX_FEATURES})"
        )
    if y_arr.shape[0] != n:
        raise InputError(
            f"y 长度 {y_arr.shape[0]} 与 X 的样本数 {n} 不一致"
        )
    return X_arr, y_arr


def validate_huber_params(
    delta: Any,
    reg_lambda: Any,
    tol: Any,
    max_iter: Any,
    fit_intercept: Any,
) -> tuple[float, float, float, int, bool]:
    """校验 Huber 拟合的超参数，返回规范化后的值。"""
    if not isinstance(delta, (int, float)) or isinstance(delta, bool):
        raise InputError("delta 必须是数值（正数），或字符串 \"auto\"")
    if not isinstance(reg_lambda, (int, float)) or isinstance(
        reg_lambda, bool
    ):
        raise InputError("reg_lambda 必须是非负数值")
    if not isinstance(tol, (int, float)) or isinstance(tol, bool):
        raise InputError("tol 必须是正数")
    if not isinstance(max_iter, int) or isinstance(max_iter, bool):
        raise InputError("max_iter 必须是正整数")
    if not isinstance(fit_intercept, bool):
        raise InputError("fit_intercept 必须是布尔值")

    delta_f = float(delta)
    reg_f = float(reg_lambda)
    tol_f = float(tol)
    max_iter_i = int(max_iter)

    _check_scalar_range(delta_f, DELTA_MIN, DELTA_MAX, "delta")
    _check_scalar_range(reg_f, 0.0, LAMBDA_MAX, "reg_lambda")
    _check_scalar_range(tol_f, TOL_MIN, TOL_MAX, "tol")
    if not (MAX_ITER_MIN <= max_iter_i <= MAX_ITER_UPPER):
        raise InputError(
            f"max_iter={max_iter_i} 超出允许范围 "
            f"[{MAX_ITER_MIN}, {MAX_ITER_UPPER}]"
        )
    return delta_f, reg_f, tol_f, max_iter_i, fit_intercept
