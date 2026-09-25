"""源码位置表示。

所有位置一律按 **Unicode 码点**（code point）计，与 Python 字符串索引一致
（Python 的 ``str`` 即以码点序列存储，``len(s)`` 是码点数而非字节数）。
字节偏移不使用 UTF-8，也不使用 UTF-16 代码单元。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    """源码中的半开区间 ``[start, end)``，偏移单位为 Unicode 码点。"""

    start: int
    end: int
    start_line: int  # 从 1 开始
    start_col: int   # 该行起始码点偏移，从 1 开始
    end_line: int
    end_col: int

    def __str__(self) -> str:
        if self.start_line == self.end_line:
            return f"{self.start_line}:{self.start_col}"
        return f"{self.start_line}:{self.start_col}-{self.end_line}:{self.end_col}"


class Source:
    """持有一段源码（按码点序列保存）并提供行列换算。

    换行只有 ``\\n`` 一种；``\\r`` 是普通码点。
    """

    def __init__(self, text: str):
        self.text = text
        self.codepoints = list(text)
        # 每个换行符 \n 之后第一个码点的偏移（行起始表）。
        self._line_starts = [0]
        for i, ch in enumerate(self.codepoints):
            if ch == "\n":
                self._line_starts.append(i + 1)

    @property
    def length(self) -> int:
        return len(self.codepoints)

    def line_col(self, offset: int) -> tuple[int, int]:
        """把码点偏移转换为 ``(行号, 列号)``，均从 1 开始。"""
        if offset < 0:
            offset = 0
        if offset > len(self.codepoints):
            offset = len(self.codepoints)
        # 二分定位最后一个 <= offset 的行起始。
        lo, hi = 0, len(self._line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._line_starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1, offset - self._line_starts[lo] + 1

    def span(self, start: int, end: int) -> Span:
        sl, sc = self.line_col(start)
        el, ec = self.line_col(end)
        return Span(start, end, sl, sc, el, ec)

    def line_text(self, line: int) -> str:
        """返回指定行（从 1 开始）的文本，不含行尾换行。"""
        if not 1 <= line <= len(self._line_starts):
            return ""
        ls = self._line_starts[line - 1]
        le = (
            self._line_starts[line] - 1
            if line < len(self._line_starts)
            else len(self.codepoints)
        )
        return "".join(self.codepoints[ls:le])
