"""诊断信息与编译期异常。

前端（词法/语法/编译）错误用 CompileError 抛出，携带 Span；
字节码验证错误不使用异常，而由 verifier 返回结构化 VerifyError，
因为一次验证可以收集多个函数的错误，并且还要携带“最短错误路径”。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .location import SourceText, Span


@dataclass
class Diagnostic:
    message: str
    span: Span | None = None
    kind: str = "error"          # error | warning
    hint: str | None = None

    def render(self, source: SourceText | None = None) -> str:
        """渲染成人类可读的单条诊断。"""
        if self.span is None or source is None:
            return f"{self.kind}: {self.message}"
        line, col = source.line_col(self.span.start)
        header = f"{source.filename}:{line}:{col}: {self.kind}: {self.message}"
        text = source.line_text(line)
        caret_indent = " " * (col - 1)
        width = max(1, min(self.span.end - self.span.start, len(text) - col + 1))
        body = "\n".join([
            header,
            f"  {text}",
            f"  {caret_indent}{'^' * width}" + (f"  {self.hint}" if self.hint else ""),
        ])
        return body


class SlangError(Exception):
    """本工具链所有异常的基类。"""


class CompileError(SlangError):
    """词法/语法/编译错误：至少一条诊断。"""

    def __init__(self, diagnostics: list[Diagnostic]) -> None:
        self.diagnostics = diagnostics
        super().__init__("; ".join(d.message for d in diagnostics))

    def render(self, source: SourceText | None = None) -> str:
        return "\n".join(d.render(source) for d in self.diagnostics)


class DecodeError(SlangError):
    """模块二进制损坏（变异截断/非法操作码等）。"""

    @property
    def function_index(self) -> int | None:
        return getattr(self, "_func", None)


class RuntimeErr(SlangError):
    """验证通过后解释执行时发生的运行期错误（除零、燃料耗尽等）。

    注意：栈下溢属于“验证器不变量被破坏”，由 InvariantBroken 表示，
    对所有“验证通过”的模块它都不应发生。
    """

    def __init__(self, message: str, pc: int = -1, func: str = "") -> None:
        self.pc = pc
        self.func = func
        super().__init__(message)


class InvariantBroken(SlangError):
    """验证器声称安全、解释器却观察到结构不变量被破坏（如栈下溢）。

    这是验收要求中的关键断言：通过验证的字节码绝不应走到这里。
    """


@dataclass
class ErrorPathNode:
    """最短可读错误路径上的一个跳转/块节点。"""

    pc: int
    kind: str                # entry | jump | jif-taken | jif-not-taken | fallthrough
    target: int | None = None
    src_line: int | None = None
    src_col: int | None = None
    note: str = ""


@dataclass
class VerifyError:
    """结构化验证错误。"""

    code: str                          # 机器可读错误码
    message: str                       # 人类可读信息
    function_index: int
    function_name: str
    pc: int = -1                       # 出错指令位置；解码错误时为 -1
    src_line: int | None = None
    src_col: int | None = None
    path: list[ErrorPathNode] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "function_index": self.function_index,
            "function": self.function_name,
            "pc": self.pc,
            "src_line": self.src_line,
            "src_col": self.src_col,
            "shortest_error_path": [
                {
                    "pc": n.pc,
                    "kind": n.kind,
                    "target": n.target,
                    "src_line": n.src_line,
                    "src_col": n.src_col,
                    "note": n.note,
                }
                for n in self.path
            ],
        }

    def render_path(self) -> str:
        """渲染“最短可读错误路径”。"""
        if not self.path:
            where = f"pc={self.pc}" if self.pc >= 0 else "解码阶段"
            return f"  在 {where} 处直接发现错误"
        lines = []
        for i, node in enumerate(self.path):
            src = f" (源码 {node.src_line}:{node.src_col})" if node.src_line else ""
            if node.kind == "entry":
                lines.append(f"  {i}. 进入函数 {node.note or ''} @pc={node.pc}{src}")
            elif node.kind == "jump":
                lines.append(f"  {i}. JUMP @pc={node.pc} -> {node.target}{src}")
            elif node.kind == "jif-taken":
                lines.append(f"  {i}. JIF 条件为真 @pc={node.pc} -> {node.target}{src}")
            elif node.kind == "jif-not-taken":
                lines.append(f"  {i}. JIF 条件为假 @pc={node.pc} 顺序执行 -> {node.target}{src}")
            elif node.kind == "fallthrough":
                lines.append(f"  {i}. 顺序执行 @pc={node.pc} -> {node.target}{src}")
            else:
                lines.append(f"  {i}. {node.kind} @pc={node.pc} -> {node.target}{src}")
        end_pc = self.pc if self.pc >= 0 else (self.path[-1].target or self.path[-1].pc)
        src = f" (源码 {self.src_line}:{self.src_col})" if self.src_line else ""
        lines.append(f"  {len(self.path)}. 到达 @pc={end_pc}{src}: {self.message}")
        return "\n".join(lines)
