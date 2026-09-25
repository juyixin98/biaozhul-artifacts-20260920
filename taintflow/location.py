"""源码位置类型。

从词法单元到 AST、IR 指令，所有节点都携带 ``Span``，告警路径中的每一步
都可以回溯到具体文件、行列（1 起始）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Pos:
    """源码中的一个点（行列均从 1 开始）。"""

    offset: int
    line: int
    col: int

    def as_dict(self) -> dict:
        return {"offset": self.offset, "line": self.line, "col": self.col}


@dataclass(frozen=True)
class Span:
    """一段半开区间 ``[start, end)``，``end`` 指向区间之后第一个字符。"""

    start: Pos
    end: Pos

    @property
    def line(self) -> int:
        return self.start.line

    def as_dict(self) -> dict:
        return {"start": self.start.as_dict(), "end": self.end.as_dict()}

    def __str__(self) -> str:
        return f"{self.start.line}:{self.start.col}-{self.end.line}:{self.end.col}"
