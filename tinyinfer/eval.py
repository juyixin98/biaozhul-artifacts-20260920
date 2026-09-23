"""最小 tree-walk 求值器。

用途不是完整实现语言语义，而是**实际运行**可变引用的不健全反例：
在 naive 多态（关闭值限制）下，类型检查器接受的程序会在运行期
破坏 ``true`` 这样的布尔值，最终在算术运算处崩溃。值限制开启时，
同一个程序在类型检查阶段就被拒绝，不会进入求值。

值的表示：int / bool / None(unit) / Closure / Cell。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import ast
from .errors import TinyError


class RuntimeFailure(TinyError):
    """运行期错误：除零、类型错误（naive 类型系统放过的程序会走到这里）。"""


@dataclass
class Cell:
    value: Any


class _Empty:
    """new_ref 创建的空单元的占位内容（未赋值前 deref 会报错）。"""

    def __repr__(self) -> str:  # pragma: no cover - 仅调试
        return "<empty-ref>"


_EMPTY = _Empty()


@dataclass
class Closure:
    param: str
    body: ast.Expr
    env: dict[str, Any]


class Evaluator:
    def __init__(self):
        # new_ref 是（朴素意义上的）多态原语：每次以"任意类型"调用都
        # 新建一个空单元。类型系统在值限制下会阻止其结果被一般化。
        self.env: dict[str, Any] = {
            "not": lambda b: (not b),
            "new_ref": lambda _unit: self._fresh_cell(_EMPTY),
            "assign": None,  # 赋值由语法 <- 承担；原语仅占位
        }
        self.cell_counter = 0

    def _fresh_cell(self, value: Any) -> Cell:
        self.cell_counter += 1
        return Cell(value)

    def eval(self, e: ast.Expr) -> Any:
        if isinstance(e, ast.IntLit):
            return e.value
        if isinstance(e, ast.BoolLit):
            return e.value
        if isinstance(e, ast.UnitLit):
            return None
        if isinstance(e, ast.Var):
            if e.name not in self.env:
                raise RuntimeFailure(f"运行期未绑定变量 {e.name!r}", e.span)
            return self.env[e.name]
        if isinstance(e, ast.Fun):
            return Closure(e.param, e.body, dict(self.env))
        if isinstance(e, ast.App):
            func = self.eval(e.func)
            arg = self.eval(e.arg)
            return self._apply(func, arg, e)
        if isinstance(e, ast.Let):
            # 求值器不区分多态：let 只是普通绑定；rec 需要让右值闭包
            # 在其自身环境中看到名字
            old = self.env.get(e.name, _UNSET)
            if e.rec:
                value = self.eval(e.bound)
                if isinstance(value, Closure):
                    value.env[e.name] = value
                self.env[e.name] = value
            else:
                value = self.eval(e.bound)
                self.env[e.name] = value
            try:
                return self.eval(e.body)
            finally:
                if old is _UNSET:
                    self.env.pop(e.name, None)
                else:
                    self.env[e.name] = old
        if isinstance(e, ast.If):
            cond = self.eval(e.cond)
            if not isinstance(cond, bool):
                raise RuntimeFailure("if 条件运行期不是 bool", e.cond.span)
            return self.eval(e.then if cond else e.else_)
        if isinstance(e, ast.Ref):
            return self._fresh_cell(self.eval(e.value))
        if isinstance(e, ast.Deref):
            cell = self.eval(e.target)
            if not isinstance(cell, Cell):
                raise RuntimeFailure("deref 的对象运行期不是引用", e.target.span)
            if isinstance(cell.value, _Empty):
                raise RuntimeFailure("解引用一个尚未赋值的空引用", e.target.span)
            return cell.value
        if isinstance(e, ast.Assign):
            cell = self.eval(e.target)
            if not isinstance(cell, Cell):
                raise RuntimeFailure("赋值左侧运行期不是引用", e.target.span)
            cell.value = self.eval(e.value)
            return None
        if isinstance(e, ast.BinOp):
            return self._binop(e)
        if isinstance(e, ast.Seq):
            self.eval(e.first)
            return self.eval(e.second)
        raise AssertionError(f"未实现的求值节点 {type(e).__name__}")  # pragma: no cover

    def _apply(self, func: Any, arg: Any, e: ast.App) -> Any:
        if not isinstance(func, Closure):
            # 允许 env 里的 Python 内建（not）
            if callable(func):
                return func(arg)
            raise RuntimeFailure("对非函数值进行应用", e.span)
        saved = self.env
        self.env = dict(func.env)
        self.env[func.param] = arg
        try:
            return self.eval(func.body)
        finally:
            self.env = saved

    def _binop(self, e: ast.BinOp) -> Any:
        a = self.eval(e.left)
        b = self.eval(e.right)
        op = e.op
        if op in ("+", "-", "*", "/"):
            if not isinstance(a, int) or isinstance(a, bool) \
                    or not isinstance(b, int) or isinstance(b, bool):
                raise RuntimeFailure(
                    f"算术运算 {op} 运行期收到非 int："
                    f"{_show(a)} {op} {_show(b)} —— 这正是 naive 多态"
                    f"放过的不健全程序的崩溃点",
                    e.span,
                )
            if op == "+":
                return a + b
            if op == "-":
                return a - b
            if op == "*":
                return a * b
            if b == 0:
                raise RuntimeFailure("整数除以零", e.span)
            return a // b
        # 比较
        if not isinstance(a, int) or isinstance(a, bool) \
                or not isinstance(b, int) or isinstance(b, bool):
            raise RuntimeFailure(
                f"比较运算 {op} 运行期收到非 int：{_show(a)} 与 {_show(b)}",
                e.span,
            )
        return {
            "==": a == b, "!=": a != b, "<": a < b,
            "<=": a <= b, ">": a > b, ">=": a >= b,
        }[op]


_UNSET = object()


def _show(v: Any) -> str:
    if isinstance(v, bool):
        return f"bool({v})"
    if v is None:
        return "unit"
    if isinstance(v, Cell):
        return f"ref({_show(v.value)})"
    if isinstance(v, Closure):
        return "<fun>"
    return repr(v)


def value_to_str(v: Any) -> str:
    if v is None:
        return "unit"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, Cell):
        return f"ref({value_to_str(v.value)})"
    if isinstance(v, Closure):
        return "<fun>"
    return str(v)
