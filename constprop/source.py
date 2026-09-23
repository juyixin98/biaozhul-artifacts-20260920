"""源码位置追踪与错误类型。

所有 AST 节点、IR 指令都携带 :class:`Span`，错误信息附带
``file:line:col``，这是“保留源码位置”要求的基础设施。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    """半开区间 ``[start, end)`` 的字节偏移与行列坐标（均从 1 开始）。"""

    start: int
    end: int
    start_line: int
    start_col: int
    end_line: int
    end_col: int

    def merge(self, other: "Span") -> "Span":
        """返回覆盖两个 span 的最小 span。"""
        if self.start <= other.start:
            lo, hi = self, other
        else:
            lo, hi = other, self
        return Span(
            lo.start,
            hi.end,
            lo.start_line,
            lo.start_col,
            hi.end_line,
            hi.end_col,
        )

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "start_line": self.start_line,
            "start_col": self.start_col,
            "end_line": self.end_line,
            "end_col": self.end_col,
        }


class SourceText:
    """保存源码文本并提供偏移到行列的换算。"""

    def __init__(self, text: str, filename: str = "<input>"):
        self.text = text
        self.filename = filename
        # line_starts[i] = 第 i 行（从 0 起）第一个字符的偏移
        self.line_starts: list[int] = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                self.line_starts.append(i + 1)

    def span(self, start: int, end: int) -> Span:
        def line_col(offset: int) -> tuple[int, int]:
            # 二分查找 offset 所在行
            lo, hi = 0, len(self.line_starts) - 1
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if self.line_starts[mid] <= offset:
                    lo = mid
                else:
                    hi = mid - 1
            return lo + 1, offset - self.line_starts[lo] + 1

        sl, sc = line_col(start)
        el, ec = line_col(max(start, end - 1))
        return Span(start, end, sl, sc, el, ec)

    def line_text(self, line: int) -> str:
        """返回第 line 行（从 1 起）的文本（不含换行符）。"""
        if line < 1 or line > len(self.line_starts):
            return ""
        start = self.line_starts[line - 1]
        end = self.text.find("\n", start)
        if end == -1:
            end = len(self.text)
        return self.text[start:end]


class L0Error(Exception):
    """所有可报告给用户的错误的基类，携带可选源码位置。"""

    kind = "error"

    def __init__(self, message: str, span: Span | None = None, source: SourceText | None = None):
        super().__init__(message)
        self.message = message
        self.span = span
        self.source = source

    def location(self) -> str:
        if self.span is None or self.source is None:
            name = self.source.filename if self.source else "<input>"
            return f"{name}:"
        return f"{self.source.filename}:{self.span.start_line}:{self.span.start_col}:"

    def render(self) -> str:
        """带源码上下文和 ^~~~~ 光标的多行错误报告。"""
        head = f"{self.location()} {self.kind}: {self.message}"
        if self.span is None or self.source is None:
            return head
        line = self.source.line_text(self.span.start_line)
        caret = " " * (self.span.start_col - 1) + "^"
        width = max(1, self.span.end - self.span.start)
        if self.span.end_line == self.span.start_line:
            caret = " " * (self.span.start_col - 1) + "^" + "~" * (width - 1)
        return f"{head}\n  {line}\n  {caret}"


class LexError(L0Error):
    kind = "lexical error"


class ParseError(L0Error):
    kind = "parse error"


class CompileError(L0Error):
    """CFG/SSA 构造阶段的错误（目前仅用于未定义变量的选择策略）。"""

    kind = "compile error"


class RuntimeErr(L0Error):
    """运行时错误（除零、读未定义变量等）。kind 固定以便比较错误行为。"""

    kind = "runtime error"

    def __init__(self, message: str, code: str, span: Span | None = None,
                 source: SourceText | None = None):
        super().__init__(message, span, source)
        # code 是稳定的错误分类标识，等价性比较只看 code，不看措辞
        self.code = code


DIV_ZERO = "division-by-zero"
UNDEFINED_VAR = "undefined-variable"
STACK_LIMIT = "step-limit"
