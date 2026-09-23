"""AST 节点定义。

每个节点都带 span（源码位置）。语句与表达式分两大层级。
文法（EBNF 全文见 docs/LANGUAGE.md）：

    program   = { func }
    func      = "fn" IDENT "(" params ")" [ ":" type ] block
    type      = "int" | "bool"
    block     = "{" { stmt } "}"
    stmt      = ";"
              | type IDENT "=" expr ";"
              | lvalue "=" expr ";"
              | "if" "(" expr ")" block [ "else" block ]
              | "while" "(" expr ")" block
              | "return" [ expr ] ";"
              | "print" expr ";"
              | expr ";"
    expr      = 析取式（优先级细节见 parser.py 文档）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .location import Span

# 类型名
INT = "int"
BOOL = "bool"


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
class VarRef(Expr):
    name: str = ""


@dataclass
class Assign(Expr):
    """作为表达式语句使用的赋值在 parser 中直接生成为 AssignStmt，
    保留此节点仅用于可能的嵌套场景；当前语言不开放嵌套赋值。"""
    target: Expr | None = None
    value: Expr | None = None


@dataclass
class Binary(Expr):
    op: str = ""           # + - * / % == != < <= > >= && ||
    left: Expr | None = None
    right: Expr | None = None


@dataclass
class Unary(Expr):
    op: str = ""           # - !
    operand: Expr | None = None


@dataclass
class Call(Expr):
    name: str = ""
    args: list[Expr] = field(default_factory=list)


# ---------- 语句 ----------
@dataclass
class Stmt(Node):
    pass


@dataclass
class EmptyStmt(Stmt):
    pass


@dataclass
class VarDecl(Stmt):
    type_name: str = ""
    name: str = ""
    init: Expr | None = None
    inferred: bool = False      # True: 由 `var` 声明，类型由初始化式推断


@dataclass
class AssignStmt(Stmt):
    name: str = ""
    value: Expr | None = None


@dataclass
class IfStmt(Stmt):
    cond: Expr | None = None
    then_block: "Block | None" = None
    else_block: "Block | None" = None


@dataclass
class WhileStmt(Stmt):
    cond: Expr | None = None
    body: "Block | None" = None


@dataclass
class ReturnStmt(Stmt):
    value: Expr | None = None       # None 表示 void return


@dataclass
class PrintStmt(Stmt):
    value: Expr | None = None


@dataclass
class ExprStmt(Stmt):
    expr: Expr | None = None


@dataclass
class Block(Node):
    stmts: list[Stmt] = field(default_factory=list)


# ---------- 顶层 ----------
@dataclass
class Param(Node):
    type_name: str = ""
    name: str = ""


@dataclass
class Func(Node):
    name: str = ""
    params: list[Param] = field(default_factory=list)
    ret_type: str | None = None     # None 表示 void
    body: Block | None = None


@dataclass
class Program(Node):
    funcs: list[Func] = field(default_factory=list)
