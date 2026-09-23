"""公共定义：源码位置与工具链统一错误。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    """源码位置（行列均为 1 基）。"""

    filename: str
    line: int
    col: int
    end_line: int = 0
    end_col: int = 0

    def short(self) -> str:
        return f"{self.filename}:{self.line}:{self.col}"

    def snippet(self, source_lines: list[str] | None) -> str:
        """渲染 ``行号 | 源码`` 形式的可读片段。"""
        if source_lines is None or not (1 <= self.line <= len(source_lines)):
            return ""
        text = source_lines[self.line - 1]
        gutter = f"{self.line:>4}"
        caret = " " * (len(gutter) + 3 + max(self.col - 1, 0)) + "^"
        return f"{gutter} | {text}\n{caret}"


# 错误阶段标签
PHASE_LEXER = "lexer"
PHASE_PARSER = "parser"
PHASE_COMPILER = "compiler"
PHASE_DECODE = "decode"
PHASE_VERIFIER = "verifier"
PHASE_INTERP = "interpreter"
PHASE_RESOURCE = "resource"
PHASE_SERVICE = "service"


class ToolError(Exception):
    """工具链统一错误。

    Attributes:
        phase:  出错阶段（见 ``PHASE_*`` 常量）。
        kind:   稳定的机器可读错误码，如 ``jump.oob``。
        message: 人类可读错误描述。
        span:   源码位置（可能为 None）。
        pc:     字节码偏移（可能为 None）。
        func_name: 出错函数名（可能为 None）。
        path:   :class:`errorpath.ErrorPath`（仅验证错误附带，可能为 None）。
    """

    def __init__(
        self,
        phase: str,
        kind: str,
        message: str,
        span: Span | None = None,
        pc: int | None = None,
        func_name: str | None = None,
        path: object | None = None,
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.kind = kind
        self.message = message
        self.span = span
        self.pc = pc
        self.func_name = func_name
        self.path = path

    def to_dict(self) -> dict:
        d: dict = {
            "phase": self.phase,
            "kind": self.kind,
            "message": self.message,
        }
        if self.func_name is not None:
            d["function"] = self.func_name
        if self.pc is not None:
            d["offset"] = self.pc
        if self.span is not None:
            d["location"] = {
                "file": self.span.filename,
                "line": self.span.line,
                "col": self.span.col,
            }
        if self.path is not None:
            # 延迟导入避免循环依赖
            from .errorpath import error_path_to_dict

            d["shortest_path"] = error_path_to_dict(self.path)
        return d
