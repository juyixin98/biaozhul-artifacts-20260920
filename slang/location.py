"""源码位置对象。

所有 token、AST 节点以及由它们产生的字节码指令都携带 Span，
使得验证器报错时可以一路回溯到源文件行列。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    """半开区间 [start, end)，以字节偏移计；行列在需要时按文本计算。"""

    start: int
    end: int
    line: int = 0       # 1-based 起始行
    col: int = 0        # 1-based 起始列

    def merge(self, other: "Span") -> "Span":
        """合并两个相邻/包含的 span，取最外层。"""
        lo = min(self.start, other.start)
        hi = max(self.end, other.end)
        first, second = (self, other) if self.start <= other.start else (other, self)
        return Span(lo, hi, first.line, first.col)

    def short(self) -> str:
        if self.line:
            return f"{self.line}:{self.col}"
        return f"{self.start}..{self.end}"


class SourceText:
    """持有一份源码并负责偏移 -> (行, 列) 的换算。"""

    def __init__(self, text: str, filename: str = "<input>") -> None:
        self.text = text
        self.filename = filename
        # 每行第一个字符的全局偏移
        self._line_starts = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                self._line_starts.append(i + 1)

    def line_col(self, offset: int) -> tuple[int, int]:
        # 二分找到 offset 所在行
        lo, hi = 0, len(self._line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._line_starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1, offset - self._line_starts[lo] + 1

    def span(self, start: int, end: int) -> Span:
        line, col = self.line_col(start)
        return Span(start, end, line, col)

    def line_text(self, line_no: int) -> str:
        """取第 n 行（1-based）的文本，不含行尾换行。"""
        if not 1 <= line_no <= len(self._line_starts):
            return ""
        s = self._line_starts[line_no - 1]
        e = self.text.find("\n", s)
        if e == -1:
            e = len(self.text)
        return self.text[s:e]
