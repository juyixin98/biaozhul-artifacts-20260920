"""统一的异常类型。

所有可预期的用户输入错误都抛出 :class:`CalibrationError`，
服务层据此生成结构化的错误响应，而不是让调用方收到原始堆栈。
"""

from __future__ import annotations


class CalibrationError(ValueError):
    """校准评估过程中的输入或参数错误。

    Parameters
    ----------
    code:
        机器可读的错误码，例如 ``"INVALID_PROBABILITY"``。
    message:
        面向调用方的错误说明。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        """转为可 JSON 序列化的结构。"""
        return {"code": self.code, "message": self.message}
