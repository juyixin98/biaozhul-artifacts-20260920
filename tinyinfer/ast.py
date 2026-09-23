"""抽象语法树。

每个表达式节点都带 ``span``，由解析器从 Token 的位置填充。

顶层程序结构见 :class:`Program`；所有顶层 ``let`` 在分析前会被
``lower_program`` 脱糖成嵌套的 ``let ... in ...``，因此核心 AST
只处理表达式。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .locations import Span

# ---- 类型标注 AST（程序员写的注解；与推导所用的 Type 分离） ---------------


@dataclass(frozen=True)
class Ann:
    """标注 AST 基类。"""

    span: Span


@dataclass(frozen=True)
class AnnVar(Ann):
    name: str  # 'a, 'b ...


@dataclass(frozen=True)
class AnnCon(Ann):
    name: str                     # int / bool / unit
    args: tuple["Ann", ...] = ()


@dataclass(frozen=True)
class AnnFun(Ann):
    param: "Ann"
    result: "Ann"


# ---- 表达式 ---------------------------------------------------------------


@dataclass(frozen=True)
class Expr:
    span: Span


@dataclass(frozen=True)
class IntLit(Expr):
    value: int


@dataclass(frozen=True)
class BoolLit(Expr):
    value: bool


@dataclass(frozen=True)
class UnitLit(Expr):
    pass


@dataclass(frozen=True)
class Var(Expr):
    name: str


@dataclass(frozen=True)
class Fun(Expr):
    param: str
    body: Expr
    ann: Ann | None = None  # 参数可选标注


@dataclass(frozen=True)
class App(Expr):
    func: Expr
    arg: Expr


@dataclass(frozen=True)
class Let(Expr):
    name: str
    bound: Expr
    body: Expr
    rec: bool = False
    ann: Ann | None = None  # 名称可选标注


@dataclass(frozen=True)
class If(Expr):
    cond: Expr
    then: Expr
    else_: Expr


@dataclass(frozen=True)
class Ref(Expr):
    value: Expr


@dataclass(frozen=True)
class Deref(Expr):
    target: Expr


@dataclass(frozen=True)
class Assign(Expr):
    target: Expr
    value: Expr


@dataclass(frozen=True)
class BinOp(Expr):
    op: str
    left: Expr
    right: Expr


@dataclass(frozen=True)
class Seq(Expr):
    first: Expr
    second: Expr


@dataclass(frozen=True)
class TopLet:
    name: str
    bound: Expr
    span: Span
    rec: bool = False
    ann: Ann | None = None


@dataclass(frozen=True)
class Program:
    """若干顶层 let，外加一个可选的收尾表达式。"""

    bindings: list[TopLet] = field(default_factory=list)
    final_expr: Expr | None = None
