"""预检/解包阶段的错误类型。

所有安全拒绝都抛出 :class:`UnsafeArchiveError`，携带机器可读的 ``code``
与可安全返回给调用方的 ``detail``（不含目标主机绝对路径）。
"""

from __future__ import annotations


class UnsafeArchiveError(Exception):
    """归档内容未通过安全检查，或超出配额。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class SignatureError(Exception):
    """Ed25519 签名缺失/格式错误/验签失败。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
