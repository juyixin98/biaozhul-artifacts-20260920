"""AST node definitions for TaintLang."""
from __future__ import annotations

from dataclasses import dataclass, field

from .lexer import Loc


@dataclass
class Node:
    loc: Loc = field(repr=False)


# ---------- expressions ----------


@dataclass
class Expr(Node):
    pass


@dataclass
class IntLit(Expr):
    value: int = 0


@dataclass
class StrLit(Expr):
    value: str = ""


@dataclass
class BoolLit(Expr):
    value: bool = False


@dataclass
class NilLit(Expr):
    pass


@dataclass
class Var(Expr):
    name: str = ""


@dataclass
class Call(Expr):
    name: str = ""
    args: list[Expr] = field(default_factory=list)


@dataclass
class Unary(Expr):
    op: str = ""
    operand: Expr | None = None


@dataclass
class Binary(Expr):
    op: str = ""
    left: Expr | None = None
    right: Expr | None = None


# ---------- statements ----------


@dataclass
class Stmt(Node):
    pass


@dataclass
class Block(Stmt):
    body: list[Stmt] = field(default_factory=list)


@dataclass
class Assign(Stmt):
    name: str = ""
    value: Expr | None = None


@dataclass
class ExprStmt(Stmt):
    expr: Expr | None = None


@dataclass
class If(Stmt):
    cond: Expr | None = None
    then: Stmt | None = None
    otherwise: Stmt | None = None  # Block or If or None


@dataclass
class While(Stmt):
    cond: Expr | None = None
    body: Stmt | None = None


@dataclass
class Return(Stmt):
    value: Expr | None = None  # nil when omitted


@dataclass
class FuncDef(Node):
    name: str = ""
    params: list[str] = field(default_factory=list)
    body: Block | None = None


@dataclass
class Program(Node):
    top_level: list[Stmt] = field(default_factory=list)
    funcs: dict[str, FuncDef] = field(default_factory=dict)
    func_order: list[str] = field(default_factory=list)


BUILTINS = ("source", "sink", "sanitize")
