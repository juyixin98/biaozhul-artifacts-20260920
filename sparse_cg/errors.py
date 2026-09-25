"""统一的请求校验错误类型。

核心算法层（csr/pcg）的结构性问题与 JSON API 层的参数校验问题
都通过 ``RequestError`` 表达，``code`` 字段给出机器可判读的错误码。
"""

from __future__ import annotations


class RequestError(ValueError):
    """输入非法（CSR 结构、参数取值、JSON 格式等）。

    Attributes:
        code: 机器可判读的错误码（见 README「错误码」一节）。
        message: 人类可读的诊断说明。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
