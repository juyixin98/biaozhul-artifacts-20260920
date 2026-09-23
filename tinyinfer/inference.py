"""带 let 多态的类型推导（Hindley–Milner / 算法 W）。

实现要点
========
* 替换表（substitution）实现合一，绑定变量前执行 **occurs-check**：
  若变量出现在要绑定的类型中则报错，拒绝 ``a ~ a -> a`` 这类递归类型。
* ``let x = e in b``：推导出 ``e : t`` 后，把 ``t`` 中对当前环境
  而言自由的变量一般化（generalize）成类型方案；使用 ``x`` 时
  实例化（instantiate）为全新变量，实现多次独立实例化。
* **值限制（value restriction，可关闭）**：只有语法值（函数、
  字面量、变量）右侧的 let 才能一般化。``let r = ref ...`` 属于
  计算式表达式，保持单态——这正是堵住可变引用多态漏洞所必需的。
  ``infer(..., value_restriction=False)`` 提供"naive W"行为用于
  反例演示。
* 每个重要步骤（绑定变量、合一、一般化、实例化、let/递归）都写入
  ``trace``，可用于解释推导过程。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import ast
from .errors import TypeError_
from .locations import Span
from .types import (
    BOOL, INT, UNIT, Scheme, Subst, TApp, TCon, TFun, TVar, Type,
    ann_to_type, apply, free_vars, render, render_scheme, scheme_free_vars,
    tfun, tref,
)


# ---- 轨迹事件 --------------------------------------------------------------


@dataclass
class TraceEvent:
    kind: str
    detail: str
    span: Span | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "span": self.span.to_dict() if self.span is not None else None,
        }


class OccursError(TypeError_):
    """occurs-check 失败：类型变量出现在自身展开中（递归类型）。"""


class UnifyError(TypeError_):
    """两个类型无法统一。"""


class UnboundError(TypeError_):
    """变量未绑定。"""


# ---- 内建环境 --------------------------------------------------------------

# 二元运算符：仅支持 int；返回类型决定分组
_ARITH_OPS = {"+", "-", "*", "/"}
_CMP_OPS = {"==", "!=", "<", "<=", ">", ">="}


def _initial_env() -> dict[str, Scheme]:
    a = TVar("'a")
    return {
        # 多态内建：
        #   ref     : 'a -> 'a ref              （按内容决定单元类型）
        #   new_ref : unit -> 'a ref            （空引用，用于展示多态
        #     与可变状态结合时的不健全性：无值限制时 r 被一般化为
        #     ∀'a. 'a ref，随后可存入任意类型）
        #   not     : bool -> bool
        "ref": Scheme(("'a",), tfun(a, tref(a))),
        "new_ref": Scheme(("'a",), tfun(UNIT, tref(a))),
        "not": Scheme((), tfun(BOOL, BOOL)),
        # 多态原语，供显式使用
        "assign": Scheme(
            ("'a",), tfun(tref(a), tfun(a, UNIT))
        ),
    }


# ---- 推导器 ----------------------------------------------------------------


class Inferer:
    def __init__(
        self,
        source: str | None = None,
        *,
        value_restriction: bool = True,
        annotate: bool = True,
    ):
        self.source = source
        self.value_restriction = value_restriction
        self.annotate = annotate
        self.subst: Subst = {}
        self.env: dict[str, Scheme] = _initial_env()
        self.counter = 0
        self.trace: list[TraceEvent] = []
        # 所有具名 let 绑定（含顶层与行内）按引入顺序记录：
        # (名字, 方案, span)；同一名字重复绑定时保留最新方案。
        self.all_let_bindings: list[tuple[str, Scheme, Span | None]] = []

    # ---- 工具 -------------------------------------------------------------
    def fresh(self, note: str = "") -> TVar:
        name = f"?{self.counter}"
        self.counter += 1
        v = TVar(name)
        if note:
            self.trace.append(
                TraceEvent("fresh", f"新类型变量 {name}（{note}）", None)
            )
        return v

    def _log(self, kind: str, detail: str, span: Span | None = None) -> None:
        self.trace.append(TraceEvent(kind, detail, span))

    # ---- 一般化与实例化 ----------------------------------------------------
    def generalize(self, t: Type) -> Scheme:
        """把 t 中不在当前环境自由变量集合里的变量提升为约束变量。"""
        root = apply(self.subst, t)
        env_fv: set[str] = set()
        for s in self.env.values():
            env_fv |= scheme_free_vars(s)
        genvars = tuple(sorted(free_vars(root) - env_fv))
        return Scheme(genvars, root)

    def instantiate(self, s: Scheme, why: str = "",
                    span: Span | None = None) -> Type:
        """用全新变量替换方案中的约束变量。"""
        if not s.vars:
            result = apply(self.subst, s.body)
            if why:
                self._log(
                    "instantiate",
                    f"{why}：单态使用，类型为 {render(result, self.subst)}",
                    span,
                )
            return result
        mapping = {qv: self.fresh() for qv in s.vars}

        def walk(x: Type) -> Type:
            x = apply(self.subst, x)
            if isinstance(x, TVar):
                return mapping.get(x.name, x)
            if isinstance(x, TFun):
                return TFun(walk(x.param), walk(x.result))
            if isinstance(x, TApp):
                return TApp(x.con, tuple(walk(a) for a in x.args))
            return x

        result = walk(s.body)
        # 新变量在 walk 之后可能已被合一绑定，统一做一次化约
        result = apply(self.subst, result)
        self._log(
            "instantiate",
            f"{why}：方案 {render_scheme(s, self.subst)} 实例化为 "
            f"{render(result, self.subst)}",
            span,
        )
        return result

    # ---- occurs-check 与合一 ----------------------------------------------
    def occurs(self, name: str, t: Type) -> bool:
        t = apply(self.subst, t)
        if isinstance(t, TVar):
            return t.name == name
        if isinstance(t, TFun):
            return self.occurs(name, t.param) or self.occurs(name, t.result)
        if isinstance(t, TApp):
            return any(self.occurs(name, a) for a in t.args)
        return False

    def bind(self, var: TVar, t: Type, span: Span | None) -> None:
        t = apply(self.subst, t)
        if isinstance(t, TVar) and t.name == var.name:
            return
        if self.occurs(var.name, t):
            raise OccursError(
                f"occurs-check 失败：类型变量 {render(var)} 出现在 "
                f"{render(t)} 中，无法构造递归（无限）类型",
                span,
            )
        self.subst[var.name] = t

    def unify(self, t1: Type, t2: Type, span: Span | None,
              context: str = "") -> None:
        a = apply(self.subst, t1)
        b = apply(self.subst, t2)
        ra, rb = render(a, self.subst), render(b, self.subst)
        if a == b:
            return
        if isinstance(a, TVar):
            self.bind(a, b, span)
            self._log(
                "unify",
                f"统一 {ra} 与 {rb}：{ra} := {rb}"
                + (f"（{context}）" if context else ""),
                span,
            )
            return
        if isinstance(b, TVar):
            self.bind(b, a, span)
            self._log(
                "unify",
                f"统一 {ra} 与 {rb}：{rb} := {ra}"
                + (f"（{context}）" if context else ""),
                span,
            )
            return
        if isinstance(a, TCon) and isinstance(b, TCon):
            if a.name == b.name:
                return
            raise UnifyError(
                f"类型冲突：期望 {ra}，实际得到 {rb}"
                + (f"（{context}）" if context else ""),
                span,
            )
        if isinstance(a, TFun) and isinstance(b, TFun):
            self.unify(a.param, b.param, span,
                       context + "：函数参数类型" if context else "函数参数类型")
            self.unify(a.result, b.result, span,
                       context + "：函数返回类型" if context else "函数返回类型")
            self._log("unify", f"函数类型统一成功：{ra} 与 {rb}", span)
            return
        if isinstance(a, TApp) and isinstance(b, TApp):
            if a.con != b.con or len(a.args) != len(b.args):
                raise UnifyError(
                    f"类型冲突：期望 {ra}，实际得到 {rb}"
                    + (f"（{context}）" if context else ""),
                    span,
                )
            for x, y in zip(a.args, b.args):
                self.unify(x, y, span, context)
            self._log("unify", f"构造类型统一成功：{ra} 与 {rb}", span)
            return
        raise UnifyError(
            f"类型冲突：期望 {ra}，实际得到 {rb}"
            + (f"（{context}）" if context else ""),
            span,
        )

    # ---- 语法值判定（值限制） ----------------------------------------------
    def is_syntactic_value(self, e: ast.Expr) -> bool:
        return isinstance(e, (ast.Fun, ast.IntLit, ast.BoolLit,
                              ast.UnitLit, ast.Var))

    def _can_generalize(self, bound: ast.Expr) -> bool:
        """严格值限制：只有右值是语法值时才允许一般化。"""
        return (not self.value_restriction) or self.is_syntactic_value(bound)

    # ---- 主推导 -----------------------------------------------------------
    def infer(self, e: ast.Expr, expected: Type | None = None) -> Type:
        t = self._infer(e)
        if expected is not None:
            self.unify(expected, t, e.span, "标注约束")
        return apply(self.subst, t)

    def _infer(self, e: ast.Expr) -> Type:  # noqa: C901 - 表达式分派，集中更清晰
        # --- 字面量 ---
        if isinstance(e, ast.IntLit):
            return INT
        if isinstance(e, ast.BoolLit):
            return BOOL
        if isinstance(e, ast.UnitLit):
            return UNIT

        # --- 变量：实例化类型方案 ---
        if isinstance(e, ast.Var):
            s = self.env.get(e.name)
            if s is None:
                raise UnboundError(f"未绑定的变量 {e.name!r}", e.span)
            return self.instantiate(s, why=f"使用变量 {e.name}", span=e.span)

        # --- 函数 ---
        if isinstance(e, ast.Fun):
            pt: Type
            if e.ann is not None:
                pt = ann_to_type(e.ann)
            else:
                pt = self.fresh(f"函数 {self._describe(e)} 的参数")
            saved_env = self.env
            self.env = {**self.env, e.param: Scheme.monomorphic(pt)}
            self._log("bind", f"参数 {e.param} : {render(pt, self.subst)}",
                      e.span)
            try:
                rt = self._infer(e.body)
            finally:
                self.env = saved_env
            ft = apply(self.subst, tfun(pt, rt))
            self._log(
                "fun",
                f"函数推出 {render(ft, self.subst)}",
                e.span,
            )
            return ft

        # --- 应用 ---
        if isinstance(e, ast.App):
            ft = self._infer(e.func)
            at = self._infer(e.arg)
            rt = self.fresh("应用结果")
            ft_root = apply(self.subst, ft)
            # 函数位置已确定为非函数类型（实参数目过多等）：
            # 错误指向函数子表达式；否则参数不匹配指向实参。
            bad_func = not isinstance(ft_root, (TFun, TVar))
            target_span = e.func.span if bad_func else e.arg.span
            self.unify(ft, tfun(at, rt), target_span,
                       context=f"应用 {self._describe(e)}")
            return apply(self.subst, rt)

        # --- let（含 rec）---
        if isinstance(e, ast.Let):
            return self._infer_let(e)

        # --- if ---
        if isinstance(e, ast.If):
            ct = self._infer(e.cond)
            self.unify(ct, BOOL, e.cond.span, "if 条件必须是 bool")
            tt = self._infer(e.then)
            et = self._infer(e.else_)
            self.unify(tt, et, e.else_.span, "if 两个分支类型必须一致")
            return apply(self.subst, tt)

        # --- 引用操作 ---
        if isinstance(e, ast.Ref):
            vt = self._infer(e.value)
            return tref(vt)
        if isinstance(e, ast.Deref):
            rt = self._infer(e.target)
            a = self.fresh("deref 内容类型")
            self.unify(rt, tref(a), e.target.span, "deref 的对象必须是引用")
            return apply(self.subst, a)
        if isinstance(e, ast.Assign):
            rt = self._infer(e.target)
            vt = self._infer(e.value)
            self.unify(rt, tref(vt), e.span,
                       f"赋值 {self._describe(e.target)} <- ...："
                       f"引用内容类型与右值不兼容")
            return UNIT

        # --- 二元运算 ---
        if isinstance(e, ast.BinOp):
            return self._infer_binop(e)

        # --- 序列 ---
        if isinstance(e, ast.Seq):
            t1 = self._infer(e.first)
            t2 = self._infer(e.second)
            self._log(
                "seq",
                f"序列左半类型 {render(t1, self.subst)}（被丢弃），"
                f"整体为 {render(t2, self.subst)}",
                e.span,
            )
            return t2

        raise AssertionError(f"未实现的表达式节点 {type(e).__name__}")  # pragma: no cover

    # ---- let 的推导（本语言多态的核心） ------------------------------------
    def _infer_let(self, e: ast.Let) -> Type:
        outer = self.env.get(e.name)
        ann_scheme: Scheme | None = None
        if e.ann is not None and self.annotate:
            ann_t = ann_to_type(e.ann)
            ann_scheme = Scheme(tuple(sorted(free_vars(ann_t))), ann_t)

        if e.rec:
            # 递归（且无标注）：名称在推导右值时是单态的（Damas–Milner
            # 不支持多态递归；多态递归需标注）。
            if ann_scheme is not None:
                bt: Type = self.instantiate(
                    ann_scheme, why=f"递归定义 {e.name} 的标注", span=e.span
                )
                self.env[e.name] = Scheme.monomorphic(bt)
                actual = self._infer(e.bound)
                self.unify(bt, actual, e.bound.span,
                           f"递归定义 {e.name} 与其标注不符")
                scheme = ann_scheme
                generalized = False
            else:
                bt0 = self.fresh(f"递归名称 {e.name} 的单态类型")
                self.env[e.name] = Scheme.monomorphic(bt0)
                actual = self._infer(e.bound)
                self.unify(bt0, actual, e.bound.span, f"递归定义 {e.name}")
                actual = apply(self.subst, actual)
                generalized = self._can_generalize(e.bound)
                scheme = (
                    self.generalize(actual) if generalized
                    else Scheme.monomorphic(actual)
                )
            self._log(
                "let-rec",
                f"递归绑定 {e.name} : {render_scheme(scheme, self.subst)}"
                + ("" if generalized else "（值限制：保持单态）"),
                e.span,
            )
        else:
            bt_raw = self._infer(e.bound)
            if ann_scheme is not None:
                # 标注只起约束作用：实际类型必须能与标注统一。
                # 最终方案仍从实际类型一般化，避免标注变量被替换链消解。
                self.unify(ann_scheme.body, bt_raw, e.bound.span,
                           f"定义 {e.name} 与其标注不符")
            actual = apply(self.subst, bt_raw)
            generalized = self._can_generalize(e.bound)
            if generalized:
                scheme = self.generalize(actual)
                tag = "[naive] 忽略值限制，" if not self.value_restriction else ""
                self._log(
                    "generalize",
                    f"{tag}一般化 {e.name} : "
                    f"{render_scheme(scheme, self.subst)}",
                    e.span,
                )
            else:
                scheme = Scheme.monomorphic(actual)
                self._log(
                    "no-generalize",
                    f"值限制：{e.name} 的右值是计算式表达式，不一般化，"
                    f"保持单态 {render(actual, self.subst)}",
                    e.span,
                )

        self.env[e.name] = scheme
        # 记录具名绑定（含行内 let），供服务展示；用当前替换化约方案体
        recorded = Scheme(scheme.vars, apply(self.subst, scheme.body))
        self.all_let_bindings.append((e.name, recorded, e.span))
        try:
            body_t = self._infer(e.body)
        finally:
            # 恢复外层绑定（顶层脱糖的最终体是 unit/表达式；恢复保证
            # 嵌套作用域正确，bindings 结果已提前记录在方案对象中）
            if outer is None:
                self.env.pop(e.name, None)
            else:
                self.env[e.name] = outer
        return apply(self.subst, body_t)

    # ---- 二元运算 ----------------------------------------------------------
    def _infer_binop(self, e: ast.BinOp) -> Type:
        lt = self._infer(e.left)
        rt = self._infer(e.right)
        if e.op in _ARITH_OPS:
            self.unify(lt, INT, e.left.span, f"运算符 {e.op} 的左操作数")
            self.unify(rt, INT, e.right.span, f"运算符 {e.op} 的右操作数")
            return INT
        if e.op in _CMP_OPS:
            self.unify(lt, rt, e.right.span,
                       f"运算符 {e.op} 两侧类型必须一致")
            # 本语言比较仅定义在 int 上（相等也仅 int，保持简单）
            self.unify(lt, INT, e.left.span, f"运算符 {e.op} 只能用于 int")
            return BOOL
        raise UnboundError(f"未知运算符 {e.op!r}", e.span)  # pragma: no cover

    def _describe(self, e: ast.Expr) -> str:
        if isinstance(e, ast.Var):
            return e.name
        if isinstance(e, ast.App) and isinstance(e.func, ast.Var):
            return e.func.name
        return type(e).__name__.lower()


# ---- 顶层程序脱糖与入口 -----------------------------------------------------


def lower_program(p: ast.Program) -> ast.Expr | None:
    """把顶层 let 序列脱糖为嵌套 let；无收尾表达式时退化为 unit。"""
    if not p.bindings and p.final_expr is None:
        return None
    expr: ast.Expr = (
        p.final_expr
        if p.final_expr is not None
        else ast.UnitLit(span=p.bindings[-1].span)
    )
    for b in reversed(p.bindings):
        expr = ast.Let(
            span=b.span, name=b.name, bound=b.bound, body=expr,
            rec=b.rec, ann=b.ann,
        )
    return expr


@dataclass
class InferResult:
    type: Type
    subst: Subst
    bindings: list[tuple[str, Scheme, Span | None]]
    trace: list[TraceEvent] = field(default_factory=list)

    def type_str(self) -> str:
        return render(self.type, self.subst)

    def binding_schemes(self) -> list[tuple[str, str]]:
        # 同名重复绑定只保留最内层（最后出现）的方案，顺序按首次引入
        order: list[str] = []
        latest: dict[str, Scheme] = {}
        for name, scheme, _span in self.bindings:
            if name not in latest:
                order.append(name)
            latest[name] = scheme
        return [(name, render_scheme(latest[name], self.subst))
                for name in order]


def infer_program(
    p: ast.Program,
    *,
    value_restriction: bool = True,
    annotate: bool = True,
    source: str | None = None,
) -> InferResult:
    """推导整个程序；返回最终表达式类型、所有具名绑定的方案与轨迹。"""
    expr = lower_program(p)
    assert expr is not None  # 解析器已拒绝空程序
    inf = Inferer(source=source, value_restriction=value_restriction,
                  annotate=annotate)
    final_t = inf.infer(expr)
    return InferResult(
        type=apply(inf.subst, final_t),
        subst=inf.subst,
        bindings=inf.all_let_bindings,
        trace=inf.trace,
    )
