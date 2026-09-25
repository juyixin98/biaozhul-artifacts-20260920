"""ToyLang 抽象语法树节点。

每个节点都带 ``span``，记录它在源文件中的 [起始偏移, 结束偏移) 与行/列。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Span:
    file: str
    start_off: int
    end_off: int
    start_line: int
    start_col: int
    end_line: int
    end_col: int

    def as_dict(self) -> dict:
        return {
            "file": self.file,
            "start": [self.start_line, self.start_col],
            "end": [self.end_line, self.end_col],
            "start_off": self.start_off,
            "end_off": self.end_off,
        }


# ---------------- 表达式 ----------------

@dataclass
class Expr:
    span: Span


@dataclass
class IntLit(Expr):
    value: int


@dataclass
class BoolLit(Expr):
    value: bool


@dataclass
class VarRef(Expr):
    name: str


@dataclass
class Unary(Expr):
    op: str          # "-" / "!"
    operand: Expr


@dataclass
class Binary(Expr):
    op: str          # + - * / % < <= > >= == != && ||
    left: Expr
    right: Expr


# ---------------- 语句 ----------------

@dataclass
class Stmt:
    span: Span


@dataclass
class VarDecl(Stmt):
    name: str
    init: Expr | None
    name_span: Span


@dataclass
class Assign(Stmt):
    name: str
    value: Expr
    name_span: Span


@dataclass
class If(Stmt):
    cond: Expr
    then_body: list[Stmt]
    else_body: list[Stmt] | None
    cond_span: Span


@dataclass
class While(Stmt):
    cond: Expr
    body: list[Stmt]
    cond_span: Span


@dataclass
class Return(Stmt):
    value: Expr | None
    value_span: Span | None = None


@dataclass
class ExprStmt(Stmt):
    expr: Expr


# ---------------- 顶层 ----------------

@dataclass
class FuncParam:
    name: str
    name_span: Span


@dataclass
class Function:
    name: str
    params: list[FuncParam]
    body: list[Stmt]
    span: Span
    name_span: Span


@dataclass
class Program:
    functions: list[Function]
    span: Span = field(
        default_factory=lambda: Span("<src>", 0, 0, 1, 1, 1, 1)
    )
