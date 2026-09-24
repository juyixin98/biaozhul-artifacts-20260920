"""AST node definitions for the scripting language."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union


@dataclass(frozen=True)
class Location:
    line: int
    col: int

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.line}:{self.col}"


# -- expressions -------------------------------------------------------------

@dataclass(frozen=True)
class IntLit:
    value: int
    loc: Location


@dataclass(frozen=True)
class StrLit:
    value: str
    loc: Location


@dataclass(frozen=True)
class BoolLit:
    value: bool
    loc: Location


@dataclass(frozen=True)
class NilLit:
    loc: Location


@dataclass(frozen=True)
class Variable:
    name: str
    loc: Location


@dataclass(frozen=True)
class UnaryOp:
    op: str                 # "-" or "not" / "!"
    operand: "Expr"
    loc: Location


@dataclass(frozen=True)
class BinaryOp:
    op: str
    left: "Expr"
    right: "Expr"
    loc: Location


@dataclass(frozen=True)
class Call:
    callee: str
    args: list["Expr"]
    loc: Location


Expr = Union[IntLit, StrLit, BoolLit, NilLit, Variable, UnaryOp, BinaryOp, Call]


# -- statements --------------------------------------------------------------

@dataclass(frozen=True)
class VarDecl:
    name: str
    init: Optional[Expr]
    loc: Location


@dataclass(frozen=True)
class Assign:
    name: str
    value: Expr
    loc: Location


@dataclass(frozen=True)
class ExprStmt:
    expr: Expr
    loc: Location


@dataclass(frozen=True)
class ReturnStmt:
    value: Optional[Expr]
    loc: Location


@dataclass(frozen=True)
class IfStmt:
    cond: Expr
    then_body: list["Stmt"]
    else_body: list["Stmt"]
    loc: Location


@dataclass(frozen=True)
class WhileStmt:
    cond: Expr
    body: list["Stmt"]
    loc: Location


Stmt = Union[VarDecl, Assign, ExprStmt, ReturnStmt, IfStmt, WhileStmt]


@dataclass(frozen=True)
class FuncDecl:
    name: str
    params: list[str]
    body: list[Stmt]
    loc: Location
    param_locs: list[Location] = field(default_factory=list)


@dataclass(frozen=True)
class Program:
    functions: dict[str, FuncDecl]
    order: list[str]  # declaration order, for stable output
    entry: str
