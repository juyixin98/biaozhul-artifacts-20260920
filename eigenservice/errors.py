"""失败类别定义。

服务对每一种失败给出稳定的机器可读 ``code``，调用方与测试据此分类，
而不是依赖错误消息文本。
"""

from __future__ import annotations


class EigenserviceError(Exception):
    """所有服务侧错误的基类。

    Attributes:
        code: 稳定的失败类别标识。
        message: 面向调用方的解释。
        details: 附加上下文 (会原样进入 HTTP 响应与日志)。
    """

    code: str = "internal_error"

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class InvalidMatrixError(EigenserviceError):
    """非方阵 / 非有限数值 / 规模越界 / 无法解析为矩阵。"""

    code = "invalid_matrix"


class AsymmetryError(EigenserviceError):
    """相对容差下不满足对称性。"""

    code = "asymmetric_matrix"


class NonConvergenceError(EigenserviceError):
    """迭代预算耗尽仍未收敛。

    绝不能仅因迭代停止就返回成功 —— 预算耗尽属于本类失败，
    响应中会附带最后一次残差等证据，结论标记为不确定。
    """

    code = "not_converged"


class QualityCheckError(EigenserviceError):
    """计算完成但后验证据 (残差 / 正交性 / 重构) 不达标。"""

    code = "quality_check_failed"
