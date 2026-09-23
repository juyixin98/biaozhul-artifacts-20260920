"""统一的错误体系。

所有面向用户的错误都携带 :class:`~tinyinfer.locations.Span`，
便于服务端与 CLI 给出冲突表达式的源码位置。
"""
from __future__ import annotations

from .locations import Span


class TinyError(Exception):
    """带源码位置的错误基类。"""

    def __init__(self, message: str, span: Span | None = None):
        super().__init__(message)
        self.message = message
        self.span = span

    def render(self, source: str | None = None) -> str:
        if self.span is None:
            return self.message
        loc = f"{self.span.start.line}:{self.span.start.column}"
        out = [f"{self.message}  (位于 {loc})"]
        if source is not None:
            snippet = self.span.snippet(source)
            if snippet:
                out.extend(["", snippet])
        return "\n".join(out)


class LexError(TinyError):
    """词法错误：非法字符、未终止注释等。"""


class ParseError(TinyError):
    """语法错误。"""


class TypeError_(TinyError):
    """类型错误：统一失败、occurs-check 命中、未绑定变量等。"""
