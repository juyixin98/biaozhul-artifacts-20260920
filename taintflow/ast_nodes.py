"""AST 定义。

所有节点都带 ``span``（来自源 Token，位置在解析器中逐节点保留）。
解析器只做语法工作；函数是否定义、参数个数是否匹配等由 IR 构建/分析阶段检查。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .location import Span


@dataclass(frozen=True)
class Expr:
    span: Span


@dataclass(frozen=True)
class NumberLit(Expr):
    value: int


@dataclass(frozen=True)
class StringLit(Expr):
    value: str


@dataclass(frozen=True)
class BoolLit(Expr):
    value: bool


@dataclass(frozen=True)
class Name(Expr):
    name: str


@dataclass(frozen=True)
class Call(Expr):
    name: str
    args: List[Expr]
    name_span: Span  # 被调用名字自身的位置，便于错误定位


@dataclass(frozen=True)
class Unary(Expr):
    op: str
    operand: Expr


@dataclass(frozen=True)
class Binary(Expr):
    op: str
    left: Expr
    right: Expr


# ---------------- 语句 ----------------


@dataclass(frozen=True)
class Stmt:
    span: Span


@dataclass(frozen=True)
class Assign(Stmt):
    target: str
    value: Expr
    target_span: Span


@dataclass(frozen=True)
class ExprStmt(Stmt):
    expr: Expr


@dataclass(frozen=True)
class Return(Stmt):
    value: Optional[Expr]


@dataclass(frozen=True)
class If(Stmt):
    cond: Expr
    then_body: List[Stmt]
    else_body: List[Stmt]


@dataclass(frozen=True)
class While(Stmt):
    cond: Expr
    body: List[Stmt]


@dataclass(frozen=True)
class Block(Stmt):
    """块在需要表达式语句的位置不出现；这里仅供复合语句统一类型。"""

    body: List[Stmt] = field(default_factory=list)


@dataclass(frozen=True)
class Function:
    name: str
    params: List[str]
    body: List[Stmt]
    span: Span
    name_span: Span


@dataclass(frozen=True)
class Program:
    functions: List[Function]
    span: Span
