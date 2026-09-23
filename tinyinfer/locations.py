"""源码位置（file / offset / 行列）与片段渲染。

位置在词法分析阶段创建并随 Token 流转，解析器把它挂到每个 AST 节点上，
类型检查阶段产生的错误因此可以精确定位到**冲突的子表达式**。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Position:
    offset: int
    line: int    # 1 起
    column: int  # 1 起

    def to_dict(self) -> dict:
        return {"offset": self.offset, "line": self.line, "column": self.column}


@dataclass(frozen=True)
class Span:
    """半开区间 [start, end)，end 是排他位置（1 起行列）。"""
    start: Position
    end: Position
    file: str = "<input>"

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "start": self.start.to_dict(),
            "end": self.end.to_dict(),
        }

    def snippet(self, source: str) -> str:
        """渲染带 ``^`` 指示的两行源码片段。"""
        if not source:
            return ""
        line_start = source.rfind("\n", 0, self.start.offset) + 1
        line_end = source.find("\n", self.end.offset)
        if line_end == -1:
            line_end = len(source)
        code_line = source[line_start:line_end]
        if not code_line.strip():
            return ""
        marker = " " * (self.start.column - 1) + "^" * max(
            1, self.end.column - self.start.column
        )
        # 跨行时只画到行尾
        marker = marker[: len(code_line)] if len(marker) > len(code_line) else marker
        return f"{self.start.line:>5} | {code_line}\n      | {marker}"


def merge(a: Span, b: Span) -> Span:
    return Span(start=a.start, end=b.end, file=a.file)
