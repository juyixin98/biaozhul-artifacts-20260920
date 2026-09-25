"""异常类型与输入限制常量。

所有面向 JSON 接口的输入错误都会抛出 :class:`RequestError`，
其 ``code`` 为稳定的机器可读错误码，``field`` 指向出错字段（JSON 指针风格）。
"""

from __future__ import annotations

# ---- 输入规模硬限制（防止滥用，可在 README 中查看）---------------------------
MAX_NODES = 2000
MAX_EDGES = 20000
MAX_PARALLEL_EDGES = 50
MAX_ID_LEN = 64
MAX_WEIGHT = 1.0e9
MIN_WEIGHT = 0.0

# 求解参数范围
MAX_ATOL = 1.0e-2
MIN_ATOL = 0.0
MAX_RTOL = 1.0e-2
MIN_RTOL = 0.0

# 默认标签上限
DEFAULT_NODE_LABEL_CAP = 10000
DEFAULT_TOTAL_LABEL_CAP = 500000

# 简单路径模式的建议规模上限（实际强制上限）
SIMPLE_MODE_MAX_NODES = 64
SIMPLE_MODE_MAX_EDGES = 400

# 暴力枚举（仅测试/校验用）的规模上限
ENUM_MAX_NODES = 24
ENUM_MAX_EDGES = 200
ENUM_MAX_PATHS = 200_000


class MosppError(Exception):
    """库层错误基类。"""


class RequestError(MosppError):
    """请求内容错误（可预期的失败状态）。

    Attributes:
        code: 稳定错误码，例如 ``"invalid_request"``。
        message: 面向人类的错误说明。
        field: 出错字段路径，例如 ``"graph.edges[2].time"``。
    """

    def __init__(self, code: str, message: str, field: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field

    def to_dict(self) -> dict:
        d = {"error": self.code, "message": self.message}
        if self.field is not None:
            d["field"] = self.field
        return d


class LimitExceeded(MosppError):
    """输入超过硬规模限制。"""

    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.code = "limit_exceeded"
        self.message = message
        self.field = field

    def to_dict(self) -> dict:
        d = {"error": self.code, "message": self.message}
        if self.field is not None:
            d["field"] = self.field
        return d
