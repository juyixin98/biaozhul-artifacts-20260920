"""VLang 的 AST 定义。所有节点都携带源码位置。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .common import Span


@dataclass
class Node:
    span: Span = field(repr=False, default=None)


# ---------- 表达式 ----------

@dataclass
class IntLit(Node):
    value: int = 0


@dataclass
class BoolLit(Node):
    value: bool = False


@dataclass
class VarRef(Node):
    name: str = ""


@dataclass
class CallExpr(Node):
    name: str = ""
    args: list = field(default_factory=list)


@dataclass
class Unary(Node):
    op: str = ""
    operand: Node = None


@dataclass
class Binary(Node):
    op: str = ""
    left: Node = None
    right: Node = None


# ---------- 语句 ----------

@dataclass
class VarDecl(Node):
    type: str = ""
    name: str = ""
    init: Node = None          # 可能为 None：声明时允许不初始化


@dataclass
class Assign(Node):
    name: str = ""
    value: Node = None


@dataclass
class ExprStmt(Node):
    expr: Node = None


@dataclass
class IfStmt(Node):
    cond: Node = None
    then_body: list = field(default_factory=list)
    else_body: list = field(default_factory=list)


@dataclass
class WhileStmt(Node):
    cond: Node = None
    body: list = field(default_factory=list)


@dataclass
class ReturnStmt(Node):
    value: Node = None         # None 表示无返回值的 return


@dataclass
class FuncDecl(Node):
    name: str = ""
    ret_type: str = "void"
    params: list[tuple[str, str]] = field(default_factory=list)  # [(类型, 名字)]
    body: list = field(default_factory=list)
