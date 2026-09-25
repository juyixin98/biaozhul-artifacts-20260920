"""AST node definitions.

Every node stores the source :class:`~resflow.locations.Span` it was parsed
from, which later propagates into CFG nodes and findings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .locations import Span


# ---------- Expressions ----------

@dataclass
class Expr:
    span: Span


@dataclass
class BoolLit(Expr):
    value: bool


@dataclass
class IntLit(Expr):
    value: int


@dataclass
class StrLit(Expr):
    value: str


@dataclass
class NullLit(Expr):
    pass


@dataclass
class VarRef(Expr):
    name: str


@dataclass
class UnaryOp(Expr):
    op: str          # only "!"
    operand: Expr


@dataclass
class BinaryOp(Expr):
    op: str          # "&&" or "||"
    left: Expr
    right: Expr


@dataclass
class CallExpr(Expr):
    name: str
    args: List[Expr]


# ---------- Statements ----------

@dataclass
class Stmt:
    span: Span


@dataclass
class Block(Stmt):
    statements: List[Stmt]


@dataclass
class AcquireStmt(Stmt):
    """let x = acquire("kind");"""
    target: str
    kind: str


@dataclass
class LetCallStmt(Stmt):
    """let x = f(...);"""
    target: str
    call: CallExpr


@dataclass
class ExprStmt(Stmt):
    call: CallExpr


@dataclass
class ReleaseStmt(Stmt):
    target: str


@dataclass
class UseStmt(Stmt):
    target: str


@dataclass
class ReturnStmt(Stmt):
    value: Optional[Expr]


@dataclass
class ThrowStmt(Stmt):
    message: str


@dataclass
class IfStmt(Stmt):
    cond: Expr
    then_block: Block
    else_block: Optional[Block]


@dataclass
class WhileStmt(Stmt):
    cond: Expr
    body: Block


@dataclass
class TryStmt(Stmt):
    try_block: Block
    error_var: str
    catch_block: Block


# ---------- Top level ----------

@dataclass
class Function:
    span: Span
    name: str
    params: List[str]
    throws: bool
    body: Block


@dataclass
class Program:
    span: Span
    functions: List[Function] = field(default_factory=list)
