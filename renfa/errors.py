"""统一错误类型。

解析阶段抛出 :class:`RegexSyntaxError`；NFA 构造超出资源上限时抛出
:class:`RegexCompileError`。二者都是 :class:`RegexError` 的子类，
JSON 服务据此返回 HTTP 400。
"""

from dataclasses import dataclass, field

from .source import Source, Span


class RegexError(Exception):
    """本引擎所有可预期错误的基类。"""


@dataclass
class RegexSyntaxError(RegexError):
    """语法/词法错误，携带源码位置。"""

    message: str
    span: Span | None = None
    source: Source | None = field(default=None, repr=False)

    def __str__(self) -> str:
        if self.span is None or self.source is None:
            return self.message
        line, col = self.span.start_line, self.span.start_col
        head = f"正则语法错误: {self.message} (第 {line} 行, 第 {col} 列)"
        line_text = self.source.line_text(line)
        if not line_text:
            return head
        # ^ 指向出错起始列（ASCII 下列号与视觉列一致；非 ASCII 码点可能更宽）。
        caret_indent = " " * (col - 1)
        return f"{head}\n  |\n  | {line_text}\n  | {caret_indent}^"


class RegexCompileError(RegexError):
    """编译期资源限制错误（例如重复展开后 NFA 状态数超限）。"""
