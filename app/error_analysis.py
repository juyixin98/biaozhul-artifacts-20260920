"""可解释误差：实测误差（对高精度参考）与经典一阶理论界。

理论界（float64，eps = 2^-52）：

- 朴素法   |E| <= (n-1) * eps * sum|x_i|            —— 随 n 线性增长
- 配对法   |E| <= ceil(log2 n) * eps * sum|x_i|     —— 随 n 对数增长
- 补偿法   |E| <= (2*eps + n*eps^2) * sum|x_i|      —— Kahan 经典界，近似与 n 无关

条件数 kappa = sum|x_i| / |sum x_i| 解释"为什么这个输入难"：
大数相消时 kappa 巨大，任何方法的相对误差都会被放大 kappa 倍量级。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Sequence

import mpmath
import numpy as np

from app.kernels import Method
from app.reference import DEFAULT_DPS, high_precision_sum, high_precision_sum_abs

EPS = float(np.finfo(np.float64).eps)


def condition_number(values: Sequence[float], dps: int = DEFAULT_DPS) -> float:
    """kappa = sum|x| / |sum x|；sum x == 0 且 sum|x| > 0 时为 inf。"""
    sum_abs = high_precision_sum_abs(values, dps)
    total = high_precision_sum(values, dps)
    if total == 0:
        return math.inf if sum_abs > 0 else 1.0
    return float(sum_abs / abs(total))


def theoretical_bound(method: Method, n: int, sum_abs: float, result: float) -> float:
    """各方法的一阶绝对误差界（上式）。sum_abs 用高精度累加后转 float。"""
    if method is Method.NAIVE:
        return max(n - 1, 0) * EPS * sum_abs
    if method is Method.PAIRWISE:
        return math.ceil(math.log2(max(n, 2))) * EPS * sum_abs
    if method is Method.COMPENSATED:
        return (2.0 * EPS + n * EPS * EPS) * sum_abs
    raise ValueError(f"未知方法: {method!r}")


@dataclass(frozen=True)
class ErrorReport:
    """单个方法的误差证据：实测 + 理论界 + 可解释字段。"""

    method: str
    result_repr: str
    reference_repr: str
    abs_error: float
    rel_error: float | None  # 参考为 0 时无意义，置 None
    ulp_error: float | None  # 结果非有限时为 None
    condition_number: float
    bound: float
    bound_kind: str
    within_bound: bool

    def as_dict(self) -> dict:
        return asdict(self)


def build_error_report(
    method: Method,
    result: float,
    values: Sequence[float],
    dps: int = DEFAULT_DPS,
) -> ErrorReport:
    """对有限结果生成误差报告；非有限结果（±Inf）由服务层另行处理。"""
    with mpmath.workdps(dps):
        ref = high_precision_sum(values, dps)
        sum_abs = float(high_precision_sum_abs(values, dps))
        abs_err = abs(mpmath.mpf(result) - ref)
        abs_err_f = float(abs_err)
        rel_err = float(abs_err / abs(ref)) if ref != 0 else None

    ulp_err: float | None = None
    if math.isfinite(result) and result != 0.0:
        ulp_err = abs_err_f / math.ulp(result)

    kappa = math.inf if ref == 0 and sum_abs > 0 else (float(sum_abs / abs(ref)) if ref != 0 else 1.0)
    bound = theoretical_bound(method, len(values), sum_abs, result)

    return ErrorReport(
        method=method.value,
        result_repr=repr(result),
        reference_repr=mpmath.nstr(ref, 30),
        abs_error=abs_err_f,
        rel_error=rel_err,
        ulp_error=ulp_err,
        condition_number=kappa,
        bound=bound,
        bound_kind=f"{method.value}_first_order",
        within_bound=abs_err_f <= bound * (1.0 + 1e-9),
    )
