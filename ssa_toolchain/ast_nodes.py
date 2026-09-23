"""AST node definitions for the mini language.

The grammar itself is documented in README.md and in ``LANGUAGE.md``.
Each node carries a :class:`~ssa_toolchain.errors.Loc` so IR built from it
can preserve source positions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .errors import Loc


@dataclass(frozen=True)
class Node:
    loc: Loc


# ---------------------------------------------------------------- expressions

@dataclass(frozen=True)
class IntLit(Node):
    value: int


@dataclass(frozen=True)
class BoolLit(Node):
    value: bool


@dataclass(frozen=True)
class Name(Node):
    name: str


@dataclass(frozen=True)
class Unary(Node):
    op: str            # "-", "!", "~"
    arg: object


@dataclass(frozen=True)
class Binary(Node):
    op: str            # + - * / % == != < <= > >= & | ^ << >> && ||
    left: object
    right: object


@dataclass(frozen=True)
class Call(Node):
    name: str
    args: tuple


# ---------------------------------------------------------------- statements

@dataclass(frozen=True)
class VarDecl(Node):
    name: str
    init: object


@dataclass(frozen=True)
class Assign(Node):
    name: str
    value: object


@dataclass(frozen=True)
class If(Node):
    cond: object
    then_body: tuple
    else_body: tuple


@dataclass(frozen=True)
class While(Node):
    cond: object
    body: tuple


@dataclass(frozen=True)
class Return(Node):
    value: Optional[object]


@dataclass(frozen=True)
class ExprStmt(Node):
    expr: object


@dataclass(frozen=True)
class Block(Node):
    stmts: tuple


@dataclass(frozen=True)
class Func(Node):
    name: str
    params: tuple          # tuple[str]
    body: tuple
    loc_end: Loc = field(default=None, compare=False)  # type: ignore


@dataclass(frozen=True)
class Program(Node):
    funcs: tuple
