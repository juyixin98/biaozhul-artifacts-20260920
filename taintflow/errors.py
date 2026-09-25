"""统一错误类型。

词法/语法错误携带源码位置，JSON 服务将其结构化为 400 响应而非抛出栈轨迹。
"""

from __future__ import annotations

from typing import Optional

from .location import Span


class TaintflowError(Exception):
    """所有 taintflow 错误的基类。"""


class LexError(TaintflowError):
    def __init__(self, message: str, span: Optional[Span] = None):
        self.message = message
        self.span = span
        loc = f" ({span})" if span else ""
        super().__init__(f"词法错误{loc}: {message}")


class ParseError(TaintflowError):
    def __init__(self, message: str, span: Optional[Span] = None):
        self.message = message
        self.span = span
        loc = f" ({span})" if span else ""
        super().__init__(f"语法错误{loc}: {message}")


class AnalysisError(TaintflowError):
    """语义层面的错误（如调用未定义函数且未开启保守模式时）。"""

    def __init__(self, message: str, span: Optional[Span] = None):
        self.message = message
        self.span = span
        loc = f" ({span})" if span else ""
        super().__init__(f"分析错误{loc}: {message}")
