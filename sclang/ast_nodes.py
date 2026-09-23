"""Abstract syntax tree for ScL.

Every node carries a :class:`~sclang.errors.Span` so later stages can
report errors at exact source locations. The resolver annotates nodes in
place (``res`` attributes); those fields stay ``None`` until
:class:`~sclang.resolver.Resolver` runs.
"""

from dataclasses import dataclass, field
from typing import Optional

from .errors import Span
from .lexer import Token


# -- statements -------------------------------------------------------------

class Stmt:
    span: Span
    res: object = None


@dataclass
class Program(Stmt):
    body: list[Stmt]
    span: Span
    res: object = None


@dataclass
class Block(Stmt):
    body: list[Stmt]
    span: Span
    res: object = None
    # names of function declarations hoisted to the block's scope
    hoisted: list[str] = field(default_factory=list)


@dataclass
class Let(Stmt):
    name: str
    init: Optional["Expr"]
    name_span: Span
    span: Span
    res: object = None


@dataclass
class FunctionStmt(Stmt):
    name: str
    params: list[str]
    body: Block
    name_span: Span
    span: Span
    res: object = None


@dataclass
class If(Stmt):
    cond: "Expr"
    then: Block
    otherwise: Optional[Block]
    span: Span
    res: object = None


@dataclass
class While(Stmt):
    cond: "Expr"
    body: Block
    span: Span
    res: object = None


@dataclass
class Return(Stmt):
    value: Optional["Expr"]
    span: Span
    res: object = None


@dataclass
class ExprStmt(Stmt):
    expr: "Expr"
    span: Span
    res: object = None


# -- expressions ------------------------------------------------------------

class Expr:
    span: Span
    res: object = None


@dataclass
class IntLit(Expr):
    value: int
    span: Span
    res: object = None


@dataclass
class StrLit(Expr):
    value: str
    span: Span
    res: object = None


@dataclass
class BoolLit(Expr):
    value: bool
    span: Span
    res: object = None


@dataclass
class NilLit(Expr):
    span: Span
    res: object = None


@dataclass
class Var(Expr):
    name: str
    span: Span
    res: object = None


@dataclass
class Assign(Expr):
    target: Var
    value: Expr
    span: Span
    res: object = None


@dataclass
class Binary(Expr):
    op: str           # "+", "-", "*", "/", "%", "==", "!=", "<", "<=", ">", ">=", "and", "or"
    left: Expr
    right: Expr
    op_span: Span
    span: Span
    res: object = None


@dataclass
class Unary(Expr):
    op: str           # "-", "not"
    operand: Expr
    op_span: Span
    span: Span
    res: object = None


@dataclass
class Call(Expr):
    callee: Expr
    args: list[Expr]
    span: Span
    res: object = None


@dataclass
class FunExpr(Expr):
    params: list[str]
    body: Block
    span: Span
    res: object = None


@dataclass
class PrintExpr(Expr):
    args: list[Expr]
    span: Span
    res: object = None


def token_name(tok: Token) -> str:
    """Human-readable token text for parser error messages."""
    if tok.kind.name in ("INT", "STRING", "IDENT"):
        return repr(tok.value)
    return tok.value if isinstance(tok.value, str) else tok.kind.name.lower()
