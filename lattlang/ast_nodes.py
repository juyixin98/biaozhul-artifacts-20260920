"""Abstract syntax tree for LattLang.

The grammar (see README / docs/syntax.md)::

    program    := statement*
    statement  := assign | print | if | while | block
    assign     := IDENT ":=" expr ";"?
    print      := "print" expr ";"?
    if         := "if" expr block ("else" (if | block))?
    while      := "while" expr block
    block      := "{" statement* "}"
    expr       := logicOr
    logicOr    := logicAnd ("||" logicAnd)*
    logicAnd   := equality ("&&" equality)*
    equality   := relational (("==" | "!=") relational)*
    relational := additive (("<" | "<=" | ">" | ">=") additive)*
    additive   := unary (("+" | "-") unary)*
    unary      := ("!" | "-") unary | primary
    primary    := INT | "true" | "false" | IDENT | "(" expr ")"

Booleans are integers (0 / 1); ``&&`` and ``||`` do NOT short-circuit, so
both operand expressions are always evaluated (this matters for preserving
observable side effects such as division by zero).
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import Span


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
class Unary:
    op: str  # "-" | "!"
    value: "Expr"
    span: Span


@dataclass(frozen=True)
class Binary:
    op: str  # + - * / % == != < <= > >= && ||
    left: "Expr"
    right: "Expr"
    span: Span


Expr = IntLit | BoolLit | Var | Unary | Binary


@dataclass(frozen=True)
class Assign:
    target: str
    target_span: Span
    value: Expr
    span: Span


@dataclass(frozen=True)
class Print:
    value: Expr
    span: Span


@dataclass(frozen=True)
class If:
    cond: Expr
    then: list["Stmt"]
    else_: list["Stmt"]
    span: Span


@dataclass(frozen=True)
class While:
    cond: Expr
    body: list["Stmt"]
    span: Span


Stmt = Assign | Print | If | While


@dataclass(frozen=True)
class Program:
    body: list[Stmt]
    span: Span
