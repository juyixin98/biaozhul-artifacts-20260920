"""Abstract environments and abstract evaluation.

An :class:`Env` maps

* scalar variables to integer intervals;
* array names to ``(size, summary)`` where ``summary`` is a single interval
  abstracting **every** element of the array (weak updates / array smashing).

``Env`` is immutable-ish: every transform returns a new object; states feed a
worklist fixpoint engine.
"""

from . import intervals as iv
from . import ast_nodes as ast


class Env:
    __slots__ = ("vars", "arrays", "bot")

    def __init__(self, vars_=None, arrays=None, bot=False):
        self.vars = vars_ if vars_ is not None else {}
        self.arrays = arrays if arrays is not None else {}
        self.bot = bot

    @staticmethod
    def initial(scalar_names, arr_decls, input_bounds=None):
        """Build the entry environment.

        ``input_bounds`` maps an input scalar name to a (lo, hi) pair used to
        bound the exhaustive/differential analysis.  An input without a bound
        starts at TOP — the analysis must then report anything it cannot prove
        safe rather than treating the unknown interval as safe.
        """
        input_bounds = input_bounds or {}
        vmap = {}
        for name, decl in scalar_names.items():
            if decl.is_input:
                b = input_bounds.get(name)
                vmap[name] = iv.TOP if b is None else iv.make(b[0], b[1])
            elif decl.init is None:
                vmap[name] = iv.TOP
            else:
                vmap[name] = iv.const(decl.init)
        amap = {a.name: (a.size, _summary(a)) for a in arr_decls}
        return Env(vmap, amap)

    def is_bottom(self):
        return self.bot

    def copy(self):
        return Env(dict(self.vars), dict(self.arrays), self.bot)

    def set_var(self, name, val):
        if iv.is_bot(val):
            return BOT_ENV
        e = self.copy()
        e.bot = False
        e.vars[name] = val
        return e

    def set_summary(self, name, val):
        e = self.copy()
        size, _ = e.arrays[name]
        e.arrays[name] = (size, iv.join(_summary_of(e.arrays[name]), val))
        return e

    def join(self, other):
        if other is None or other.bot:
            return self
        if self.bot:
            return other
        vs = {}
        for k in self.vars:
            vs[k] = iv.join(self.vars[k], other.vars.get(k, iv.BOT))
        ar = {}
        for k, (size, s) in self.arrays.items():
            osize, os_ = other.arrays.get(k, (size, iv.BOT))
            ar[k] = (size, iv.join(s, os_))
        return Env(vs, ar)

    def subset_of(self, other):
        if self.bot:
            return True
        if other.bot:
            return False
        for k, v in self.vars.items():
            if not iv.subset(v, other.vars.get(k, iv.BOT)):
                return False
        for k, (size, s) in self.arrays.items():
            osize, os_ = other.arrays.get(k, (size, iv.BOT))
            if size != osize or not iv.subset(s, os_):
                return False
        return True

    def widen(self, other):
        if other is None or other.bot:
            return self
        if self.bot:
            return other
        vs = {k: iv.widen(self.vars[k], other.vars[k]) for k in self.vars}
        ar = {k: (size, iv.widen(s, other.arrays[k][1]))
              for k, (size, s) in self.arrays.items()}
        return Env(vs, ar)

    def widen_selective(self, other, widen_vars):
        """Widen only variables in ``widen_vars``; all other scalars and all
        array summaries take the plain join. Used at a loop header so that
        variables not modified inside that loop (e.g. an outer induction
        variable passing through an inner header) are not spuriously widened.
        """
        if other is None or other.bot:
            return self
        if self.bot:
            return other
        vs = {}
        for k in self.vars:
            nv = other.vars.get(k, iv.BOT)
            vs[k] = iv.widen(self.vars[k], nv) if k in widen_vars \
                else iv.join(self.vars[k], nv)
        ar = {}
        for k, (size, s) in self.arrays.items():
            osize, os_ = other.arrays.get(k, (size, iv.BOT))
            ar[k] = (size, iv.join(s, os_))
        return Env(vs, ar)

    def narrow(self, other):
        if self.bot or other.bot:
            return self
        vs = {k: iv.narrow(self.vars[k], other.vars.get(k, iv.BOT))
              for k in self.vars}
        ar = {k: (size, iv.narrow(s, other.arrays.get(k, (size, iv.BOT))[1]))
              for k, (size, s) in self.arrays.items()}
        candidate = Env(vs, ar)
        # Narrowing must produce a smaller post-fixpoint. If the per-interval
        # refinement (computed from possibly not-yet-reconverged incoming
        # edges) ever escapes the current state, the current state is the
        # sound value and is kept unchanged.
        if any(iv.is_bot(v) for v in vs.values()) or not candidate.subset_of(self):
            return self
        return candidate

    def __eq__(self, other):
        if not isinstance(other, Env):
            return NotImplemented
        if self.bot != other.bot:
            return False
        return self.bot or (self.vars == other.vars and self.arrays == other.arrays)

    def __hash__(self):
        if self.bot:
            return hash("BOT_ENV")
        return hash((tuple(sorted(self.vars.items())),
                     tuple(sorted((k, size, s)
                                  for k, (size, s) in self.arrays.items()))))

    def to_dict(self):
        if self.bot:
            return {"bottom": True}
        return {
            "scalars": {k: _iv_json(v) for k, v in sorted(self.vars.items())},
            "arrays": {k: {"size": size, "elements": _iv_json(s)}
                       for k, (size, s) in sorted(self.arrays.items())},
        }


BOT_ENV = Env(bot=True)


def _summary(a: ast.ArrDecl):
    acc = iv.BOT
    for x in a.elems:
        acc = iv.join(acc, iv.const(x))
    return acc


def _summary_of(pair):
    return pair[1]


def _iv_json(v):
    if iv.is_bot(v):
        return {"bottom": True}
    lo, hi = v
    return {"lo": None if lo is None else str(lo),
            "hi": None if hi is None else str(hi)}


# --------------------------------------------------------- expression eval

class EvalResult:
    """Interval of an expression plus alarms raised while evaluating it."""
    __slots__ = ("value", "alarms")

    def __init__(self, value, alarms=None):
        self.value = value
        self.alarms = alarms or []


def eval_expr(env: Env, e):
    """Abstract-evaluate an integer expression, collecting div/mod and OOB
    alarms at the exact source locations of the offending operations.

    This evaluator works on AST nodes over user variables/arrays (used for
    branch conditions).  The CFG transfer in :mod:`intervalai.analyzer` has its
    own instruction-level evaluator over temporaries.
    """
    alarms = []

    def rec(node):
        if isinstance(node, ast.IntLit):
            return iv.const(node.value)
        if isinstance(node, ast.Var):
            return env.vars.get(node.name, iv.TOP)
        if isinstance(node, ast.ArrayRef):
            idx = rec(node.index)
            size, summary = env.arrays[node.name]
            _index_alarms(node, idx, size, alarms)
            # A possibly-OOB read yields an unknown value (see analyzer
            # LoadElem); only a provably in-bounds read is the element summary.
            if _index_may_be_invalid(idx, size):
                return iv.TOP
            return summary
        if isinstance(node, ast.Unary):
            v = rec(node.expr)
            return iv.neg(v) if node.op == "-" else v  # bool not handled here
        if isinstance(node, ast.Binary):
            a = rec(node.lhs)
            b = rec(node.rhs)
            op = node.op
            if op == "+":
                return iv.add(a, b)
            if op == "-":
                return iv.sub(a, b)
            if op == "*":
                return iv.mul(a, b)
            if op == "/":
                q, may_zero = iv.div(a, b)
                if may_zero:
                    alarms.append(_alarm("div_by_zero", node, a, b))
                    return iv.join(q, iv.TOP)
                return q
            if op == "%":
                r, may_zero = iv.mod(a, b)
                if may_zero:
                    alarms.append(_alarm("div_by_zero", node, a, b))
                    return iv.join(r, iv.TOP)
                return r
        raise AssertionError(f"non-integer expression in eval: {type(node).__name__}")

    value = rec(e)
    return EvalResult(value, alarms)


def condition_alarms(env: Env, e):
    """Collect OOB/div alarms from the integer subexpressions of a boolean
    condition (the condition itself has no numeric value to compute)."""
    alarms = []

    def walk(node):
        if isinstance(node, ast.BoolLit):
            return
        if isinstance(node, ast.Unary) and node.op in ("not", "!"):
            walk(node.expr)
            return
        if isinstance(node, ast.Binary) and node.op in ("&&", "and", "||", "or"):
            walk(node.lhs)
            walk(node.rhs)
            return
        if isinstance(node, ast.Binary) and node.op in (
                "==", "!=", "<", "<=", ">", ">="):
            alarms.extend(eval_expr(env, node.lhs).alarms)
            alarms.extend(eval_expr(env, node.rhs).alarms)
            return
        # Unexpected shape — evaluate directly, best effort.
        alarms.extend(eval_expr(env, node).alarms)

    walk(e)
    return alarms


def _index_may_be_invalid(idx, size):
    if iv.is_bot(idx):
        return False
    lo, hi = idx
    return lo is None or lo < 0 or hi is None or hi >= size


def _index_alarms(node, idx, size, alarms):
    """OOB alarms for an array index.

    Negative indices are always illegal (Imp has no Python-style negative
    indexing); indices >= size are illegal.  Any uncertainty stays an alarm:
    an unknown interval is never treated as safe.
    """
    if iv.is_bot(idx):
        return
    lo, hi = idx
    if lo is None or lo < 0:
        alarms.append({
            "kind": "index_out_of_bounds",
            "subkind": "negative_index_possible",
            "loc": node.loc.to_dict(),
            "size": size,
            "index": _iv_json(idx),
            "message": "array index may be negative",
        })
    if hi is None or hi >= size:
        alarms.append({
            "kind": "index_out_of_bounds",
            "subkind": "upper_bound_possible",
            "loc": node.loc.to_dict(),
            "size": size,
            "index": _iv_json(idx),
            "message": f"array index may be >= array size {size}",
        })


def _alarm(kind, node, a, b):
    return {
        "kind": kind,
        "subkind": "zero_divisor_possible",
        "loc": node.loc.to_dict(),
        "divisor": _iv_json(b),
        "message": "right operand of division may be zero",
    }


# ------------------------------------------------------------- assume/filter

def assume(env: Env, e, negate=False):
    """Refine ``env`` with the boolean condition ``e`` (or its negation).

    Returns BOT-marked env when the condition is infeasible.  The refinement
    handles comparisons of a variable against a constant (and, when one side
    is a singleton, variable vs variable) — the classic interval narrowing
    rules — and is sound: unrefined structure is always left unchanged.
    """
    if env.is_bottom():
        return env
    refined = _filter(env, e, polarity=not negate)
    return refined


def _filter(env, e, polarity):
    # polarity True  => constrain e to be true
    # polarity False => constrain e to be false
    if isinstance(e, ast.BoolLit):
        if e.value == polarity:
            return env
        return _bot_env(env)
    if isinstance(e, ast.Unary) and e.op in ("not", "!"):
        return _filter(env, e.expr, not polarity)
    if isinstance(e, ast.Binary) and e.op in ("&&", "and"):
        if polarity:
            return _filter(_filter(env, e.lhs, True), e.rhs, True)
        # !(a && b) = !a || !b: join the two feasible refinements
        return _safe_join(_filter(env, e.lhs, False),
                          _filter(env, e.rhs, False))
    if isinstance(e, ast.Binary) and e.op in ("||", "or"):
        if polarity:
            return _safe_join(_filter(env, e.lhs, True),
                              _filter(env, e.rhs, True))
        return _filter(_filter(env, e.lhs, False), e.rhs, False)
    if isinstance(e, ast.Binary) and e.op in _CMP_OPS:
        op = e.op
        if not polarity:
            op = {"==": "!=", "!=": "==", "<": ">=", "<=": ">",
                  ">": "<=", ">=": "<"}[op]
        return _filter_cmp(env, e.lhs, e.rhs, op)
    # No refinement available: leave env unchanged (sound).
    return env


_CMP_OPS = ("==", "!=", "<", "<=", ">", ">=")


def _safe_join(a, b):
    # Both branches feasible sets; join; if either side went bottom keep the
    # other.
    if a.is_bottom():
        return b
    if b.is_bottom():
        return a
    return a.join(b)


def _bot_env(env):
    return BOT_ENV


def _filter_cmp(env, lhs, rhs, op):
    """Refine the environment under an integer comparison.

    Three shapes are recognised:

    * a scalar variable compared with a constant **expression** (a literal or
      an expression whose abstract value is a singleton);
    * two scalar variables (bound propagation, see :func:`_two_var`);
    * anything else (no refinement — sound).
    """
    lr = eval_expr(env, lhs).value
    rr = eval_expr(env, rhs).value

    lhs_is_var = isinstance(lhs, ast.Var)
    rhs_is_var = isinstance(rhs, ast.Var)
    lhs_const = _is_const_expr(env, lhs)
    rhs_const = _is_const_expr(env, rhs)

    if lhs_is_var and rhs_const:
        return _refine_var(env, lhs.name, lr, rr, op, side="left")
    if rhs_is_var and lhs_const:
        return _refine_var(env, rhs.name, rr, lr, _flip(op), side="left")
    if lhs_is_var and rhs_is_var:
        return _two_var(env, lhs.name, lr, rhs.name, rr, op)
    return env


def _is_const_expr(env, e):
    """True only if ``e`` provably denotes a single fixed integer.

    A *bounded but non-singleton* variable is deliberately not a constant —
    mistaking it for one is unsound.
    """
    if isinstance(e, ast.IntLit):
        return True
    if isinstance(e, ast.Var):
        v = env.vars.get(e.name)
        return v is not None and not iv.is_bot(v) and v[0] == v[1]
    if isinstance(e, ast.Unary) and e.op == "-":
        return _is_const_expr(env, e.expr)
    if isinstance(e, ast.Binary) and e.op in ("+", "-", "*"):
        return _is_const_expr(env, e.lhs) and _is_const_expr(env, e.rhs)
    return False


def _flip(op):
    return {"==": "==", "!=": "!=", "<": ">", "<=": ">=",
            ">": "<", ">=": "<="}[op]


def _refine_var(env, name, cur, other, op, side):
    """Apply x op c where other = [c0,c1] constant interval.

    We intersect the current interval with the implied half-line; equality
    intersects with the whole interval; disequality can only remove a singleton
    point when both sides are singletons (otherwise no useful interval bound).
    """
    lo, hi = other
    if op == "==":
        new = iv.meet(cur, other)
    elif op == "!=":
        if lo == hi and cur == iv.const(lo):
            new = iv.BOT
        elif lo == hi and iv.contains(cur, lo):
            # Remove the single point c.
            clo, chi = cur
            if clo == lo:
                new = iv.make(lo + 1, chi)
            elif chi == lo:
                new = iv.make(clo, lo - 1)
            else:
                new = cur  # interior hole not representable: keep both
        else:
            new = cur
    else:
        bound = {"<":  (None, hi - 1 if hi is not None else None),
                 "<=": (None, hi),
                 ">":  (lo + 1 if lo is not None else None, None),
                 ">=": (lo, None)}[op]
        new = iv.meet(cur, bound)
    return env.set_var(name, new) if not iv.is_bot(new) else _bot_env(env)


def _two_var(env, x, xv, y, yv, op):
    """Bound propagation across a comparison of two variables.

    The interval domain cannot express the relation itself, but each
    variable can inherit a bound from the other's current bound::

        x <  y => x <= hi(y)-1   and y >= lo(x)+1
        x <= y => x <= hi(y)     and y >= lo(x)
        x >  y => x >= lo(y)+1   and y <= hi(x)-1
        x >= y => x >= lo(y)     and y <= hi(x)

    Equality/inequality propagate only through singleton bounds (a real
    relational domain would be needed for anything stronger).  Rules that
    involve an infinite counterpart bound impose no constraint.
    """
    out = env
    xlo, xhi = xv
    ylo, yhi = yv

    if op == "==":
        # Equal => both must lie in the meet of the two intervals.
        m = iv.meet(xv, yv)
        if iv.is_bot(m):
            return BOT_ENV
        out = out.set_var(x, m)
        out = out.set_var(y, m)
        return out
    if op == "!=":
        return out

    # implied constraint on x from y's bounds, and on y from x's bounds
    x_bounds = {
        "<":  (None, yhi - 1 if yhi is not None else None),
        "<=": (None, yhi),
        ">":  (ylo + 1 if ylo is not None else None, None),
        ">=": (ylo, None),
    }[op]
    y_bounds = {
        "<":  (xlo + 1 if xlo is not None else None, None),
        "<=": (xlo, None),
        ">":  (None, xhi - 1 if xhi is not None else None),
        ">=": (None, xhi),
    }[op]

    new_x = iv.meet(xv, x_bounds)
    new_y = iv.meet(yv, y_bounds)
    if iv.is_bot(new_x) or iv.is_bot(new_y):
        return BOT_ENV
    out = out.set_var(x, new_x)
    out = out.set_var(y, new_y)
    return out
