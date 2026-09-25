"""错误类型。

所有面向调用方的失败都用 :class:`KalmanError` 的子类表示，并携带稳定的
``code`` 字符串，供 JSON 接口与上层程序做分支处理。
"""

from __future__ import annotations


class KalmanError(Exception):
    """本项目所有异常的基类。"""

    code = "kalman_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class DimensionError(KalmanError):
    """矩阵/向量维度不匹配。"""

    code = "dimension_mismatch"


class InvalidValueError(KalmanError):
    """输入不是有限实数（NaN、inf）或超出允许的数值范围。"""

    code = "invalid_value"


class MatrixPropertyError(KalmanError):
    """矩阵不满足要求的性质（非方阵、不对称、不正定/半正定）。"""

    code = "matrix_property_violation"


class NumericalStabilityError(KalmanError):
    """数值稳定检查失败：Cholesky 分解失败或协方差不再半正定。"""

    code = "numerical_stability_failure"
