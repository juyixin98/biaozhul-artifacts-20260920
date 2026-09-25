"""A small call-by-value evaluator for MiniML.

This is deliberately *untyped* (it does not consult inferred types): it exists
to run accepted programs and — crucially — to show that the program rejected
under the value restriction actually **crashes at runtime** when unsound
generalization is allowed. ``examples/demo_unsound.py`` does exactly that:
the same source is inferred with ``value_restriction=False``, then evaluated,
and the cast via a shared reference cell raises ``TypePanic``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .ast_nodes import (
    App,
    Assign,
    BinOp,
    Binding,
    BoolLit,
    Deref,
    Expr,
    If,
    IntLit,
    Lam,
    Let,
    Paren,
    Program,
    Ref,
    Seq,
    UnaryOp,
    UnitLit,
    Var,
)
from .span import Span


class TypePanic(Exception):
    """Runtime type error (the dynamic analogue of an inference failure)."""

    def __init__(self, message: str, span: Span):
        super().__init__(message)
        self.message = message
        self.span = span


class MatchPanic(Exception):
    def __init__(self, message: str, span: Span):
        super().__init__(message)
        self.message = message
        self.span = span


@dataclass
class VFun:
    param: str
    body: Expr
    env: "Env"
    name: Optional[str] = None  # for recursive functions


@dataclass
class VRef:
    cell: list  # single-element mutable list


@dataclass
class VUnit:
    _inst: Optional["VUnit"] = None

    def __repr__(self) -> str:
        return "()"


UNIT = VUnit()


@dataclass
class VBool:
    v: bool

    def __repr__(self) -> str:
        return "true" if self.v else "false"


class Env:
    def __init__(self, parent: Optional["Env"] = None):
        self.vars: dict[str, object] = {}
        self.parent = parent

    def lookup(self, name: str) -> object:
        if name in self.vars:
            return self.vars[name]
        if self.parent is not None:
            return self.parent.lookup(name)
        raise KeyError(name)

    def child(self) -> "Env":
        return Env(self)


@dataclass
class EvalResult:
    value: Optional[object]
    printed_bindings: list[tuple[str, object]] = field(default_factory=list)
    output: str = ""


def _fmt(v: object) -> str:
    if isinstance(v, bool):  # guard before int (bool is an int subclass)
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, VUnit):
        return "()"
    if isinstance(v, VBool):
        return repr(v)
    if isinstance(v, VRef):
        return f"ref({_fmt(v.cell[0])})"
    if isinstance(v, VFun):
        return f"<fun {v.name or v.param}>"
    return repr(v)


def _as_int(v: object, span: Span, ctx: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise TypePanic(f"{ctx}: expected int but got runtime value {_fmt(v)}", span)
    return v


def _as_bool(v: object, span: Span, ctx: str) -> bool:
    if isinstance(v, VBool):
        return v.v
    if isinstance(v, bool):
        return v
    raise TypePanic(f"{ctx}: expected bool but got runtime value {_fmt(v)}", span)


def _as_ref(v: object, span: Span, ctx: str) -> VRef:
    if not isinstance(v, VRef):
        raise TypePanic(f"{ctx}: expected reference but got {_fmt(v)}", span)
    return v


def _as_fun(v: object, span: Span) -> VFun:
    if not isinstance(v, VFun):
        raise TypePanic(f"cannot apply non-function value {_fmt(v)}", span)
    return v


class Evaluator:
    def __init__(self):
        self.global_env = Env()

    def eval(self, e: Expr, env: Env) -> object:
        if isinstance(e, IntLit):
            return e.value
        if isinstance(e, BoolLit):
            return VBool(e.value)
        if isinstance(e, UnitLit):
            return UNIT
        if isinstance(e, Var):
            try:
                return env.lookup(e.name)
            except KeyError:
                raise TypePanic(f"unbound value {e.name!r}", e.span)
        if isinstance(e, Lam):
            return VFun(e.param, e.body, env)
        if isinstance(e, App):
            return self._app(e, env)
        if isinstance(e, BinOp):
            return self._binop(e, env)
        if isinstance(e, UnaryOp):
            v = self.eval(e.operand, env)
            return -_as_int(v, e.operand.span, "unary '-'")
        if isinstance(e, If):
            c = _as_bool(self.eval(e.cond, env), e.cond.span, "if condition")
            if c:
                return self.eval(e.then, env)
            if e.els is not None:
                return self.eval(e.els, env)
            return UNIT
        if isinstance(e, Let):
            return self._let(e, env)
        if isinstance(e, Ref):
            return VRef([self.eval(e.inner, env)])
        if isinstance(e, Deref):
            r = _as_ref(self.eval(e.inner, env), e.inner.span, "dereference")
            return r.cell[0]
        if isinstance(e, Assign):
            r = _as_ref(self.eval(e.target, env), e.target.span, "assignment")
            r.cell[0] = self.eval(e.value, env)
            return UNIT
        if isinstance(e, Seq):
            self.eval(e.first, env)
            return self.eval(e.second, env)
        if isinstance(e, Paren):
            return self.eval(e.inner, env)
        raise AssertionError(f"unevaluable node {e!r}")  # pragma: no cover

    def _app(self, e: App, env: Env) -> object:
        f = _as_fun(self.eval(e.fn, env), e.fn.span)
        arg = self.eval(e.arg, env)
        call_env = f.env.child()
        call_env.vars[f.param] = arg
        if f.name is not None:
            call_env.vars[f.name] = f
        return self.eval(f.body, call_env)

    def _let(self, e: Let, env: Env) -> object:
        local = env.child()
        if e.rec:
            lam = e.value
            assert isinstance(lam, Lam)  # parser guarantees
            v = self.eval(lam, local)
            v.name = e.name
            local.vars[e.name] = v
        else:
            local.vars[e.name] = self.eval(e.value, env)
        return self.eval(e.body, local)

    def _binop(self, e: BinOp, env: Env) -> object:
        op = e.op
        lv = self.eval(e.left, env)
        rv = self.eval(e.right, env)
        if op in ("+", "-", "*", "/"):
            a = _as_int(lv, e.left.span, f"operator {op!r}")
            b = _as_int(rv, e.right.span, f"operator {op!r}")
            if op == "+":
                return a + b
            if op == "-":
                return a - b
            if op == "*":
                return a * b
            if b == 0:
                raise MatchPanic("division by zero", e.span)
            # truncation towards zero, like most MLs with / on ints
            q = abs(a) // abs(b)
            return q if (a < 0) == (b < 0) else -q
        if op in ("&&", "||"):
            a = _as_bool(lv, e.left.span, f"operator {op!r}")
            b = _as_bool(rv, e.right.span, f"operator {op!r}")
            return VBool((a and b) if op == "&&" else (a or b))
        # comparisons: equality is structural for our simple values.
        if op == "=":
            return VBool(_runtime_equal(lv, rv))
        if op == "<>":
            return VBool(not _runtime_equal(lv, rv))
        if op in ("<", "<=", ">", ">="):
            a = _as_int(lv, e.left.span, f"operator {op!r}")
            b = _as_int(rv, e.right.span, f"operator {op!r}")
            return VBool(
                {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]
            )
        raise AssertionError(op)  # pragma: no cover

    def eval_program(self, p: Program) -> EvalResult:
        printed: list[tuple[str, object]] = []
        for b in p.bindings:
            v = self._eval_binding(b)
            self.global_env.vars[b.name] = v
            printed.append((b.name, v))
        final = self.eval(p.final_expr, self.global_env) if p.final_expr else None
        lines = [f"{name} = {_fmt(v)}" for name, v in printed]
        if final is not None:
            lines.append(f"- : {_fmt(final)}")
        return EvalResult(final, printed, "\n".join(lines))

    def _eval_binding(self, b: Binding) -> object:
        if b.rec:
            lam = b.value
            assert isinstance(lam, Lam)  # parser guarantees
            v = self.eval(lam, self.global_env)
            v.name = b.name
            return v
        return self.eval(b.value, self.global_env)


def _runtime_equal(a: object, b: object) -> bool:
    if isinstance(a, VBool):
        return isinstance(b, VBool) and a.v == b.v
    if isinstance(a, VUnit):
        return isinstance(b, VUnit)
    if isinstance(a, VRef):
        return isinstance(b, VRef) and _runtime_equal(a.cell[0], b.cell[0])
    if isinstance(a, VFun):
        return a is b
    if isinstance(a, bool):
        return isinstance(b, bool) and a == b
    if isinstance(a, int):
        return not isinstance(b, bool) and isinstance(b, int) and a == b
    return a is b


def eval_program(p: Program) -> EvalResult:
    return Evaluator().eval_program(p)
