"""编译器各阶段的错误类型。"""

from __future__ import annotations


class ToolchainError(Exception):
    """所有工具链错误的基类。"""


class LexError(ToolchainError):
    """词法错误。"""

    def __init__(self, message: str, line: int, col: int):
        super().__init__(f"词法错误 (行 {line}, 列 {col}): {message}")
        self.line = line
        self.col = col


class ParseError(ToolchainError):
    """语法错误。"""

    def __init__(self, message: str, line: int, col: int):
        super().__init__(f"语法错误 (行 {line}, 列 {col}): {message}")
        self.line = line
        self.col = col


class IRError(ToolchainError):
    """IR 构建错误。"""


class SSAError(ToolchainError):
    """SSA 构造/校验错误。"""


class RuntimeExecError(ToolchainError):
    """解释执行期错误（如除以零）。"""

    def __init__(self, message: str, line: int | None = None, col: int | None = None):
        loc = f" (源文件行 {line}, 列 {col})" if line is not None else ""
        super().__init__(f"运行时错误{loc}: {message}")
        self.line = line
        self.col = col
