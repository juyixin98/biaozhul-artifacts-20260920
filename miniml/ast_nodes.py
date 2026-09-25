"""AST for MiniML.

Every node carries the exact source :class:`~miniml.span.Span` it was parsed
from, so later phases (inference, evaluation) can point at the conflicting
expression in error messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .span import Span


@dataclass
class Node:
    span: Span = field(repr=False)


# ---------------------------------------------------------------- expressions


@dataclass
class Expr(Node):
    pass


@dataclass
class IntLit(Expr):
    value: int


@dataclass
class BoolLit(Expr):
    value: bool


@dataclass
class UnitLit(Expr):
    pass


@dataclass
class Var(Expr):
    name: str


@dataclass
class Lam(Expr):
    """fun x -> body  (single-argument function)"""

    param: str
    body: Expr
    param_span: Span  # span of the parameter identifier


@dataclass
class App(Expr):
    fn: Expr
    arg: Expr


@dataclass
class BinOp(Expr):
    op: str  # '+', '-', '*', '/', '=', '<>', '<', '<=', '>', '>=', '&&', '||'
    left: Expr
    right: Expr
    op_span: Span


@dataclass
class UnaryOp(Expr):
    op: str  # '-'
    operand: Expr
    op_span: Span


@dataclass
class If(Expr):
    cond: Expr
    then: Expr
    els: Optional[Expr]  # None means no else branch (body forced to unit)


@dataclass
class Let(Expr):
    """let [rec] name = value in body (inline let)"""

    name: str
    value: Expr
    body: Expr
    rec: bool
    name_span: Span


@dataclass
class Ref(Expr):
    """ref expr : allocate a mutable cell"""

    inner: Expr


@dataclass
class Deref(Expr):
    """!expr : read a mutable cell"""

    inner: Expr


@dataclass
class Assign(Expr):
    """expr := expr : write a mutable cell; evaluates to unit"""

    target: Expr
    value: Expr


@dataclass
class Seq(Expr):
    """e1 ; e2"""

    first: Expr
    second: Expr


@dataclass
class Paren(Expr):
    """A parenthesized expression; keeps the outer span for the toolchain
    while inference/evaluation treat it transparently."""

    inner: Expr


# ----------------------------------------------------------------- top level


@dataclass
class Binding(Node):
    """Top-level ``let [rec] name = expr`` (terminated by ``;;``)."""

    name: str
    value: Expr
    rec: bool
    name_span: Span


@dataclass
class Program(Node):
    bindings: list[Binding]
    # Optional trailing expression, e.g. the program ``1 + 2 ;;``
    final_expr: Optional[Expr] = None
