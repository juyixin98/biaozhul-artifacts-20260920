"""AST node definitions.

Every node carries a :class:`~taintlang.location.Span`.  The grammar the
parser accepts is documented in ``docs/grammar`` and the README::

    program     := func+
    func        := 'func' IDENT '(' params? ')' block
    block       := '{' stmt* '}'
    stmt        := 'var' IDENT ('=' expr)? ';'
                  | IDENT '=' expr ';'
                  | 'if' '(' expr ')' block ('else' block)?
                  | 'while' '(' expr ')' block
                  | 'return' expr? ';'
                  | expr ';'
    expr        := or
    or          := and ('||' and)*
    and         := equality ('&&' equality)*
    equality    := relational (('==' | '!=') relational)*
    relational  := additive (('<' | '<=' | '>' | '>=') additive)*
    additive    := multiplicative (('+' | '-') multiplicative)*
    multiplicative := unary (('*' | '/' | '%') unary)*
    unary       := ('!' | '-') unary | call
    call        := primary ('(' args? ')')?        # calls are NOT chained
    primary     := NUMBER | STRING | 'true' | 'false' | IDENT
                  | '(' expr ')'
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .location import Span


@dataclass(frozen=True)
class Expr:
    span: Span


@dataclass(frozen=True)
class NumberLit(Expr):
    value: str


@dataclass(frozen=True)
class StringLit(Expr):
    raw: str  # including surrounding quotes


@dataclass(frozen=True)
class BoolLit(Expr):
    value: bool


@dataclass(frozen=True)
class VarRef(Expr):
    name: str


@dataclass(frozen=True)
class Unary(Expr):
    op: str
    operand: Expr


@dataclass(frozen=True)
class Binary(Expr):
    op: str
    left: Expr
    right: Expr


@dataclass(frozen=True)
class Call(Expr):
    callee: str
    args: tuple[Expr, ...]


@dataclass(frozen=True)
class Stmt:
    span: Span


@dataclass(frozen=True)
class VarDecl(Stmt):
    name: str
    init: Expr | None


@dataclass(frozen=True)
class Assign(Stmt):
    name: str
    value: Expr


@dataclass(frozen=True)
class IfStmt(Stmt):
    cond: Expr
    then_block: "Block"
    else_block: "Block | None"


@dataclass(frozen=True)
class WhileStmt(Stmt):
    cond: Expr
    body: "Block"


@dataclass(frozen=True)
class ReturnStmt(Stmt):
    value: Expr | None


@dataclass(frozen=True)
class ExprStmt(Stmt):
    expr: Expr


@dataclass(frozen=True)
class Block:
    span: Span
    statements: tuple[Stmt, ...]


@dataclass(frozen=True)
class FuncDecl:
    span: Span
    name: str
    params: tuple[str, ...]
    body: Block


@dataclass(frozen=True)
class Program:
    span: Span
    functions: tuple[FuncDecl, ...]
