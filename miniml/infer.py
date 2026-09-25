"""Type inference for MiniML: Algorithm W with the value restriction.

Key design points
-----------------
* **Let-polymorphism** (Damas–Milner): a let-bound *value* (lambda, literal,
  variable, ...) gets a polymorphic :class:`~miniml.types.Scheme`; every use is
  a fresh instantiation.
* **Value restriction**: a let/top-level binding whose right-hand side is not a
  syntactic value is given a *monomorphic* type, even when its free type
  variables are not constrained by the environment. This is what keeps
  ``let r = ref []``-style generalizations sound for mutable references.
  Pass ``value_restriction=False`` to switch the restriction off and observe
  the unsoundness directly (see examples/ and the test suite).
* **Occurs check** lives in :func:`miniml.types.unify`; every unification is
  reported against the expression that caused it, so conflicts carry the
  conflicting expressions' source locations.
* A :class:`Tracer` records every constraint/unification/generalization step so
  the derivation can be inspected (``/infer`` response and ``--trace`` CLI).
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
from .types import (
    Scheme,
    TVar,
    Type,
    free_vars,
    generalize,
    instantiate,
    mk_arrow,
    mk_ref,
    t_bool,
    t_int,
    t_unit,
    type_str,
    unify,
)
from .types import UnifyError


class InferError(Exception):
    def __init__(
        self,
        message: str,
        span: Span,
        related: Optional[list[tuple[Span, str]]] = None,
        cycle: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.span = span
        self.related = related or []
        self.cycle = cycle


@dataclass
class TraceEvent:
    step: str
    detail: str
    span: Optional[Span] = None


@dataclass
class Tracer:
    events: list[TraceEvent] = field(default_factory=list)
    enabled: bool = True

    def emit(self, step: str, detail: str, span: Optional[Span] = None) -> None:
        if self.enabled:
            self.events.append(TraceEvent(step, detail, span))


# Expressions that are "simple values" for the value restriction.
# Nested let/refs inside are not required: only these syntactic forms count.
def _is_syntactic_value(e: Expr) -> bool:
    if isinstance(e, Paren):
        return _is_syntactic_value(e.inner)
    return isinstance(e, (Lam, IntLit, BoolLit, UnitLit, Var))


@dataclass
class BindingResult:
    name: str
    scheme: Scheme          # type of the binding *result*
    env_scheme: Scheme      # scheme bound to the name in the environment
    monomorphic: bool       # True when generalization was suppressed with free vars


@dataclass
class InferenceResult:
    expr_type: Optional[Type]
    bindings: list[BindingResult]
    trace: list[TraceEvent]
    env_schemes: dict[str, Scheme]


class Inferrer:
    def __init__(self, value_restriction: bool = True, trace: Optional[Tracer] = None):
        self.env: dict[str, Scheme] = {}
        self.counter = 0
        self.value_restriction = value_restriction
        self.tracer = trace or Tracer()

    # ---------------------------------------------------------- utilities
    def fresh(self, hint: str = "") -> TVar:
        self.counter += 1
        v = TVar()
        v.name = hint or f"t{self.counter}"
        return v

    def _fresh_arrow(self, hint: str = "f") -> tuple[TVar, TVar, TApp]:
        a = self.fresh("a")
        b = self.fresh("b")
        return a, b, mk_arrow(a, b)

    def instantiate_scheme(self, sch: Scheme, why: str) -> Type:
        t = instantiate(sch, lambda: self.fresh())
        fv = free_vars([t])
        ids = ", ".join(f"t{v.id}" for v in fv)
        self.tracer.emit(
            "instantiate",
            f"{why}: {sch}  =>  {type_str(t)}  (fresh: {ids or 'none'})"
        )
        return t

    def unify_at(
        self,
        actual: Type,
        expected: Type,
        span: Span,
        what: str,
        related: Optional[list[tuple[Span, str]]] = None,
    ) -> None:
        self.tracer.emit(
            "unify", f"{what}: {type_str(actual)} ~ {type_str(expected)}", span
        )
        try:
            unify(actual, expected)
        except UnifyError as exc:
            if exc.cycle:
                msg = (
                    f"{what}: occurs check failed; {exc.message}"
                )
            else:
                msg = (
                    f"{what}: expected {type_str(expected)}, "
                    f"but got {type_str(actual)}"
                )
            raise InferError(msg, span, related, cycle=exc.cycle) from exc

    # ---------------------------------------------------------- expressions
    def infer(self, e: Expr) -> Type:
        t = self._infer(e)
        self.tracer.emit(
            "inferred", f"{_expr_head(e)} : {type_str(t)}", e.span
        )
        return t

    def _infer(self, e: Expr) -> Type:
        if isinstance(e, IntLit):
            return t_int
        if isinstance(e, BoolLit):
            return t_bool
        if isinstance(e, UnitLit):
            return t_unit
        if isinstance(e, Var):
            sch = self.env.get(e.name)
            if sch is None:
                raise InferError(f"unbound value {e.name!r}", e.span)
            return self.instantiate_scheme(sch, f"variable {e.name!r}")
        if isinstance(e, Lam):
            return self._infer_lam(e)
        if isinstance(e, App):
            return self._infer_app(e)
        if isinstance(e, BinOp):
            return self._infer_binop(e)
        if isinstance(e, UnaryOp):
            return self._infer_unary(e)
        if isinstance(e, If):
            return self._infer_if(e)
        if isinstance(e, Let):
            return self._infer_let(e)
        if isinstance(e, Ref):
            inner = self.infer(e.inner)
            return mk_ref(inner)
        if isinstance(e, Deref):
            inner = self.infer(e.inner)
            a = self.fresh("a")
            self.unify_at(inner, mk_ref(a), e.inner.span, "dereference expects a reference")
            return a
        if isinstance(e, Assign):
            return self._infer_assign(e)
        if isinstance(e, Seq):
            # e1 is evaluated for its effect (if any) and discarded, so its
            # type is unconstrained; only e2's type matters.
            self.infer(e.first)
            return self.infer(e.second)
        if isinstance(e, Paren):
            return self.infer(e.inner)
        raise AssertionError(f"unhandled expression {e!r}")  # pragma: no cover

    def _infer_lam(self, e: Lam) -> Type:
        a = self.fresh(e.param)
        old = self.env.get(e.param)
        self.env[e.param] = Scheme([], a)
        self.tracer.emit("enter-lambda", f"parameter {e.param} : {type_str(a)}", e.span)
        body_t = self.infer(e.body)
        if old is None:
            self.env.pop(e.param, None)
        else:
            self.env[e.param] = old
        return mk_arrow(a, body_t)

    def _infer_app(self, e: App) -> Type:
        fn_t = self.infer(e.fn)
        arg_t = self.infer(e.arg)
        a, b, ft = self._fresh_arrow()
        self.unify_at(
            fn_t,
            ft,
            e.fn.span,
            "this expression is applied as a function",
            related=[(e.arg.span, f"argument has type {type_str(arg_t)}")],
        )
        # After unification, a/b are linked; constrain argument domain too so
        # its location is reported when the *argument* is wrong. We unify arg_t
        # with the (possibly substituted) domain type.
        self.unify_at(
            arg_t,
            a,
            e.arg.span,
            "argument type",
            related=[(e.fn.span, f"function expects {type_str(a)} here")],
        )
        return b

    def _infer_binop(self, e: BinOp) -> Type:
        op = e.op
        if op in ("+", "-", "*", "/"):
            lt = self.infer(e.left)
            rt = self.infer(e.right)
            self.unify_at(lt, t_int, e.left.span, f"left operand of {op!r}")
            self.unify_at(rt, t_int, e.right.span, f"right operand of {op!r}")
            return t_int
        if op in ("&&", "||"):
            lt = self.infer(e.left)
            rt = self.infer(e.right)
            self.unify_at(lt, t_bool, e.left.span, f"left operand of {op!r}")
            self.unify_at(rt, t_bool, e.right.span, f"right operand of {op!r}")
            return t_bool
        if op in ("=", "<>", "<", "<=", ">", ">="):
            lt = self.infer(e.left)
            rt = self.infer(e.right)
            # Equality/comparison is ad-hoc polymorphic: same type on both
            # sides; the common type is unconstrained for '=' / '<>' (equality
            # polymorphism) and required to be int for the ordering operators.
            self.unify_at(lt, rt, e.right.span, f"operands of {op!r} must match")
            if op in ("<", "<=", ">", ">="):
                self.unify_at(lt, t_int, e.left.span, f"operands of {op!r} must be integers")
            return t_bool
        raise AssertionError(f"unknown operator {op}")  # pragma: no cover

    def _infer_unary(self, e: UnaryOp) -> Type:
        t = self.infer(e.operand)
        self.unify_at(t, t_int, e.operand.span, "operand of unary '-'")
        return t_int

    def _infer_if(self, e: If) -> Type:
        ct = self.infer(e.cond)
        self.unify_at(ct, t_bool, e.cond.span, "condition of 'if'")
        tt = self.infer(e.then)
        if e.els is None:
            self.unify_at(tt, t_unit, e.then.span, "'then' branch without 'else' must be unit")
            return t_unit
        et = self.infer(e.els)
        self.unify_at(
            et, tt, e.els.span,
            "branches of 'if' must have the same type",
            related=[(e.then.span, f"'then' branch has type {type_str(tt)}")],
        )
        return tt

    def _infer_assign(self, e: Assign) -> Type:
        target_t = self.infer(e.target)
        value_t = self.infer(e.value)
        a = self.fresh("a")
        self.unify_at(
            target_t,
            mk_ref(a),
            e.target.span,
            "left side of ':=' must be a reference",
        )
        self.unify_at(value_t, a, e.value.span, "value assigned to the reference")
        return t_unit

    def _generalize_binding(self, name: str, value: Expr, value_t: Type,
                            name_span: Span, top: bool) -> BindingResult:
        syntactic_value = _is_syntactic_value(value)
        env_types = [s.body for s in self.env.values()]

        if self.value_restriction and not syntactic_value:
            # Value restriction: the name is bound to a monomorphic scheme.
            # Generalizing against every free variable of the value quantifies
            # none of them, but subsequent unifications (e.g. the first use)
            # may still constrain them.
            every = free_vars([value_t])
            sch = generalize([v for v in every], value_t)
            restricted = bool(every)
            if restricted:
                self.tracer.emit(
                    "generalize",
                    f"{name} kept monomorphic (not a syntactic value, value "
                    f"restriction): {type_str(value_t)}",
                    name_span,
                )
            else:
                self.tracer.emit(
                    "generalize",
                    f"{name} : {type_str(value_t)} (ground type, nothing to quantify)",
                    name_span,
                )
            self.env[name] = sch
            return BindingResult(name, sch, sch, restricted)

        sch = generalize(env_types, value_t)
        self.tracer.emit(
            "generalize",
            f"{name} : {type_str(value_t)}  =>  {sch}",
            name_span,
        )
        self.env[name] = sch
        return BindingResult(name, sch, sch, False)

    def _infer_let(self, e: Let) -> Type:
        if e.rec:
            # let rec f = fun x -> ... : introduce a monomorphic variable for f
            # while checking the lambda, then generalize (the lambda is a value).
            a = self.fresh(e.name)
            old = self.env.get(e.name)
            self.env[e.name] = Scheme([], a)
            self.tracer.emit("enter-rec", f"{e.name} assumed : {type_str(a)}", e.name_span)
            vt = self.infer(e.value)
            self.unify_at(vt, a, e.value.span, f"recursive definition {e.name!r}")
            # f must not count as part of the (outer) environment while its own
            # scheme is being generalized, or nothing could be quantified.
            if old is None:
                self.env.pop(e.name, None)
            else:
                self.env[e.name] = old
            br = self._generalize_binding(e.name, e.value, a, e.name_span, top=False)
            bt = self.infer(e.body)
            if old is None:
                self.env.pop(e.name, None)
            else:
                self.env[e.name] = old
            return bt

        vt = self.infer(e.value)
        old = self.env.get(e.name)
        br = self._generalize_binding(e.name, e.value, vt, e.name_span, top=False)
        bt = self.infer(e.body)
        if old is None:
            self.env.pop(e.name, None)
        else:
            self.env[e.name] = old
        return bt

    # ---------------------------------------------------------- top level
    def infer_binding(self, b: Binding) -> BindingResult:
        if b.rec:
            a = self.fresh(b.name)
            old = self.env.get(b.name)
            self.env[b.name] = Scheme([], a)
            self.tracer.emit("enter-rec", f"{b.name} assumed : {type_str(a)}", b.name_span)
            vt = self.infer(b.value)
            self.unify_at(vt, a, b.value.span, f"recursive definition {b.name!r}")
            if old is None:
                self.env.pop(b.name, None)
            else:
                self.env[b.name] = old
            return self._generalize_binding(b.name, b.value, a, b.name_span, top=True)
        vt = self.infer(b.value)
        return self._generalize_binding(b.name, b.value, vt, b.name_span, top=True)

    def infer_program(self, p: Program) -> InferenceResult:
        results: list[BindingResult] = []
        for b in p.bindings:
            results.append(self.infer_binding(b))
        final_t = self.infer(p.final_expr) if p.final_expr is not None else None
        return InferenceResult(final_t, results, list(self.tracer.events), dict(self.env))


def infer_program(
    p: Program, value_restriction: bool = True, trace: bool = True
) -> InferenceResult:
    inf = Inferrer(value_restriction=value_restriction, trace=Tracer(enabled=trace))
    return inf.infer_program(p)


_EXPR_HEADS = {
    IntLit: "integer literal",
    BoolLit: "boolean literal",
    UnitLit: "unit literal",
    Var: "variable",
    Lam: "function",
    App: "application",
    BinOp: "binary operation",
    UnaryOp: "negation",
    If: "if-expression",
    Let: "let-expression",
    Ref: "reference allocation",
    Deref: "dereference",
    Assign: "assignment",
    Seq: "sequence",
    Paren: "parenthesized expression",
}


def _expr_head(e: Expr) -> str:
    return _EXPR_HEADS.get(type(e), type(e).__name__)
