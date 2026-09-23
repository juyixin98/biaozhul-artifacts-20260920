"""Abstract syntax tree for IntervalLang.

Every node carries a `span` pointing at the source text it came from; that
location is propagated through the IR into analysis alarms.
"""

from __future__ import annotations
from dataclasses import dataclass, field

from .errors import Span


@dataclass(frozen=True)
class Program:
    decls: list["Decl"]
    body: "Block"
    span: Span


# ---- declarations ---------------------------------------------------------

@dataclass(frozen=True)
class Decl:
    name: str
    span: Span                # location of the identifier
    kind: str                 # "var" | "array" | "input"
    length: int | None = None  # arrays only
    length_span: Span | None = None
    init: "Expr | None" = None  # var initializer (defaults to 0)


# ---- statements -----------------------------------------------------------

@dataclass(frozen=True)
class Block:
    stmts: list["Stmt"]
    span: Span


@dataclass(frozen=True)
class Assign:
    """Scalar assignment  name = expr"""
    name: str
    value: "Expr"
    span: Span
    name_span: Span


@dataclass(frozen=True)
class ArrayStore:
    """Array element assignment  name[index] = expr"""
    name: str
    index: "Expr"
    value: "Expr"
    span: Span
    name_span: Span


@dataclass(frozen=True)
class If:
    cond: "Expr"
    then: Block
    else_: "Block | None"
    span: Span


@dataclass(frozen=True)
class While:
    cond: "Expr"
    body: Block
    span: Span


@dataclass(frozen=True)
class InputStmt:
    name: str
    span: Span
    name_span: Span


@dataclass(frozen=True)
class HavocStmt:
    name: str
    span: Span
    name_span: Span


@dataclass(frozen=True)
class Skip:
    span: Span


@dataclass(frozen=True)
class LocalDecl:
    """Local scalar declaration appearing among statements: var x [= e];"""
    name: str
    init: "Expr | None"
    span: Span


Stmt = Assign | ArrayStore | If | While | InputStmt | HavocStmt | Skip | LocalDecl


# ---- expressions ----------------------------------------------------------

@dataclass(frozen=True)
class IntLit:
    value: int
    span: Span


@dataclass(frozen=True)
class BoolLit:
    value: bool
    span: Span


@dataclass(frozen=True)
class Var:
    name: str
    span: Span


@dataclass(frozen=True)
class ArrayLoad:
    name: str
    index: "Expr"
    span: Span
    name_span: Span


@dataclass(frozen=True)
class Unary:
    op: str                   # "-" | "!"
    operand: "Expr"
    span: Span


@dataclass(frozen=True)
class Binary:
    op: str                   # + - * / %  < <= > >= == != && ||
    left: "Expr"
    right: "Expr"
    span: Span
    op_span: Span


Expr = IntLit | BoolLit | Var | ArrayLoad | Unary | Binary
