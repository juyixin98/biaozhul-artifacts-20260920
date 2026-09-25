"""错误类型与失败状态码。

所有失败状态都以 ``status.*`` 字符串常量返回（JSON 响应的 ``status`` 字段），
其中除 ``OK`` 外均为失败状态。
"""


class status:
    """求解状态常量。"""

    OK = "ok"
    UNREACHABLE = "unreachable"          # 源点不可到达终点
    TRUNCATED = "truncated"              # 标签数达到上限，结果可能不完整
    INVALID_REQUEST = "invalid_request"  # 输入校验失败
    LIMIT_EXCEEDED = "limit_exceeded"    # 超出小/中规模范围
    INTERNAL_ERROR = "internal_error"    # 不应出现的内部错误


class MOSPError(ValueError):
    """输入/参数校验错误。

    :ivar code: 机器可读状态码，取 :class:`status` 中的值。
    :ivar detail: 面向调用方的错误说明（中文）。
    """

    def __init__(self, detail: str, code: str = status.INVALID_REQUEST):
        super().__init__(detail)
        self.code = code
        self.detail = detail


class TruncationError(RuntimeError):
    """标签数达到 ``label_cap`` 上限时内部抛出，由 API 层转成 truncated 响应。"""
