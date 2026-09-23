"""L0 的抽象语法树定义。

语法（EBNF，完整文档见 README）::

    program     := statement*
    statement   := block | if | while | print | assign | assert
    block       := '{' statement* '}'
    if          := 'if' expr statement ('else' statement)?
    while       := 'while' expr statement
    print       := 'print' expr ';'
    assign      := IDENT '=' expr ';'
    expr        := orExpr
    orExpr      := andExpr ('||' andExpr)*
    andExpr     := cmpExpr ('&&' cmpExpr)*
    cmpExpr     := addExpr (('=='|'!='|'<'|'>'|'<='|'>=') addExpr)?
    addExpr     := mulExpr (('+'|'-') mulExpr)*
    mulExpr     := unary (('*'|'/'|'%') unary)*
    unary       := '!' unary | '-' unary | primary
    primary     := INTEGER | 'true' | 'false' | IDENT | '(' expr ')'

比较与逻辑运算结果为整数 0/1。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .source import Span


@dataclass
class Node:
    span: Span = field(repr=False)


# ---------- 表达式 ----------


@dataclass
class Expr(Node):
    pass


@dataclass
class IntLit(Expr):
    value: int = 0


@dataclass
class BoolLit(Expr):
    value: bool = False


@dataclass
class Var(Expr):
    name: str = ""


@dataclass
class Unary(Expr):
    op: str = ""       # '-' 或 '!'
    operand: Expr | None = None


@dataclass
class Binary(Expr):
    op: str = ""       # + - * / % == != < > <= >=
    left: Expr | None = None
    right: Expr | None = None


@dataclass
class Logical(Expr):
    op: str = ""       # '&&' 或 '||'，短路
    left: Expr | None = None
    right: Expr | None = None


# ---------- 语句 ----------


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
class Print(Stmt):
    value: Expr | None = None


@dataclass
class If(Stmt):
    cond: Expr | None = None
    then: Stmt | None = None
    otherwise: Stmt | None = None


@dataclass
class While(Stmt):
    cond: Expr | None = None
    body: Stmt | None = None


@dataclass
class Program(Node):
    body: list[Stmt] = field(default_factory=list)
