"""Interval and point evaluation of ASTs.

One :class:`Evaluator` is created per request with its own mpmath contexts
(``MPIntervalContext`` for range arithmetic, ``MPContext`` for high-precision
point arithmetic). Using per-request contexts avoids mutating the process-wide
``mpmath.iv`` precision from concurrent FastAPI requests.

All evaluation failures are normalised to the service error taxonomy:

* leaving the real domain (``sqrt``/``log`` of an interval reaching negative,
  a fractional power of a negative interval, division by an interval that is
  exactly zero) raises :class:`DomainEvaluationError` carrying the source
  position of the offending node;
* anything else mpmath raises becomes :class:`ComputationFailure`.
"""

from __future__ import annotations

import mpmath
from mpmath.ctx_iv import MPIntervalContext
from mpmath.ctx_mp import MPContext

from .ast import (
    Binary,
    Call,
    Const,
    Node,
    Num,
    Paren,
    Unary,
    Var,
)
from .errors import ComputationFailure, DomainEvaluationError, SourcePosition

# mpmath raises these when an interval operation meets the complex branch.
_DOMAIN_EXC_TYPES = (
    mpmath.libmp.libmpf.ComplexResult,
    ZeroDivisionError,
)


def _find_real_span(node: Node) -> tuple[int, int] | None:
    """DFS for the first descendant with a real (non-synthesised) span."""
    while isinstance(node, Paren):
        node = node.inner
    if node.start >= 0 and node.end >= 0:
        return node.start, node.end
    children: tuple[Node, ...] = ()
    if isinstance(node, Unary):
        children = (node.operand,)
    elif isinstance(node, Binary):
        children = (node.left, node.right)
    elif isinstance(node, Call):
        children = (node.arg,)
    for child in children:
        found = _find_real_span(child)
        if found is not None:
            return found
    return None


class Evaluator:
    def __init__(self, precision_dps: int) -> None:
        self.iv = MPIntervalContext()
        self.mp = MPContext()
        self.iv.dps = precision_dps
        self.mp.dps = precision_dps
        self.dps = precision_dps

    # -- public API --------------------------------------------------------
    def interval(self, node: Node, x):  # x: ivmpf
        """Return an enclosure of ``node``'s range over interval ``x``."""
        return self._eval_interval(node, x)

    def point_iv(self, node: Node, point) -> int:
        """Evaluate ``node`` at a single point, returning a thin ivmpf.

        The result is a zero-width interval: evaluating through interval
        arithmetic keeps rounding errors enclosed, so ``f(point)`` is never a
        silently rounded bare float.
        """
        thin = self.iv.mpf([point, point])
        return self._eval_interval(node, thin)

    def point_mp(self, node: Node, point):
        """Evaluate at a point with plain high-precision mpf (for SciPy seeds).

        Used only for the *approximate*, clearly-unverified root search.
        """
        return self._eval_point(node, self.mp.mpf(point))

    def make_interval(self, lo, hi):
        return self.iv.mpf([lo, hi])

    def to_mpf(self, value) -> None:
        """Convert an interval endpoint (tuple/ivmpf/str) to a plain mpf."""
        if hasattr(value, "_mpi_"):
            return self.mp.mpf(value._mpi_[0])
        return self.mp.mpf(value)

    # -- interval recursion ------------------------------------------------
    def _eval_interval(self, node: Node, x):
        node = self._unwrap(node)
        try:
            return self._do_interval(node, x)
        except DomainEvaluationError:
            raise
        except _DOMAIN_EXC_TYPES as exc:
            raise DomainEvaluationError(
                f"value-range evaluation leaves the real domain here: {exc}",
                self._position(node),
            ) from exc
        except (ValueError, OverflowError) as exc:
            raise ComputationFailure(
                f"interval evaluation failed at {type(node).__name__}: {exc}",
                {"node": type(node).__name__},
            ) from exc

    def _do_interval(self, node: Node, x):
        iv = self.iv
        if isinstance(node, Num):
            return iv.mpf(self._num_iv(node.text))
        if isinstance(node, Var):
            return x
        if isinstance(node, Const):
            return iv.pi if node.name == "pi" else iv.e
        if isinstance(node, Unary):
            v = self._eval_interval(node.operand, x)
            return v if node.op == "+" else -v
        if isinstance(node, Binary):
            return self._do_binary_interval(node, x)
        if isinstance(node, Call):
            return self._do_call_interval(node, x)
        raise ComputationFailure(f"unknown AST node {type(node).__name__}")

    def _do_binary_interval(self, node: Binary, x):
        iv = self.iv
        if node.op == "^":
            base = self._eval_interval(node.left, x)
            # The exponent is a numeric literal by grammar; evaluate it as a
            # thin constant so raising an interval to it uses exact semantics.
            exponent = self._eval_interval(node.right, iv.mpf([0, 0]))
            p = self._thin_value(exponent)
            return self._power_interval(base, p, node)
        left = self._eval_interval(node.left, x)
        right = self._eval_interval(node.right, x)
        if node.op == "+":
            return left + right
        if node.op == "-":
            return left - right
        if node.op == "*":
            return left * right
        if node.op == "/":
            return self._divide_interval(left, right, node)
        raise ComputationFailure(f"unknown operator {node.op!r}")

    def _divide_interval(self, left, right, node: Binary):
        # Extended interval division: mpmath returns infinite endpoints when
        # the divisor straddles zero (e.g. 1/[0,2] = [0.5,+inf]). That is a
        # valid conservative range, *unless* the divisor is exactly zero
        # everywhere (thin zero), which is a domain error.
        if right._mpi_[0] == right._mpi_[1] == self._zero_tuple():
            raise DomainEvaluationError(
                "division by zero: the divisor's range is exactly {0}",
                self._position(node.right),
            )
        result = left / right
        # mpmath propagates nan when 0/0 style indeterminate forms occur.
        if self._is_nan(result):
            raise DomainEvaluationError(
                "division is indeterminate on this interval (0/0 form)",
                self._position(node),
            )
        return result

    def _power_interval(self, base, p, node: Binary):
        """Interval power with a constant exponent, preserving domain info."""
        iv = self.iv
        mp = self.mp
        # Integer exponent: exact for all bases, via repeated/mpf integer power.
        if p == self.mp.floor(p):
            k = int(self.mp.floor(p))
            return base ** k
        # Non-integer real power requires base >= 0.
        lo = mp.mpf(base._mpi_[0])
        if lo < 0:
            raise DomainEvaluationError(
                f"non-integer power {mpmath.nstr(p, 18)} is not real for a "
                "negative base interval",
                self._position(node),
            )
        exponent_thin = iv.mpf([mp.mpf(p), mp.mpf(p)])
        if lo == 0:
            # 0 ** negative is undefined; mpmath raises ZeroDivisionError.
            return base ** exponent_thin
        return base ** exponent_thin

    def _do_call_interval(self, node: Call, x):
        iv = self.iv
        v = self._eval_interval(node.arg, x)
        # log is real only on (0, inf); mpmath returns -inf at 0 instead of
        # raising, so enforce the domain explicitly and keep the position.
        if node.name == "log":
            arg_lo = self.mp.mpf(v._mpi_[0])
            if arg_lo <= 0:
                raise DomainEvaluationError(
                    "log is only real-valued for strictly positive arguments; "
                    "the argument range reaches 0 or negative values",
                    self._position(node.arg),
                )
        table = {
            "sin": iv.sin,
            "cos": iv.cos,
            "tan": iv.tan,
            "exp": iv.exp,
            "log": iv.log,
            "sqrt": iv.sqrt,
        }
        fn = table[node.name]
        result = fn(v)
        if self._is_nan(result):  # pragma: no cover - defensive
            raise DomainEvaluationError(
                f"{node.name} is not real-valued on this interval",
                self._position(node),
            )
        return result

    # -- point recursion (plain mpf, approximate pipeline only) ------------
    def _eval_point(self, node: Node, x):
        node = self._unwrap(node)
        mp = self.mp
        if isinstance(node, Num):
            return mp.mpf(node.text)
        if isinstance(node, Var):
            return x
        if isinstance(node, Const):
            return mp.pi if node.name == "pi" else mp.e
        if isinstance(node, Unary):
            v = self._eval_point(node.operand, x)
            return v if node.op == "+" else -v
        if isinstance(node, Binary):
            if node.op == "^":
                base = self._eval_point(node.left, x)
                exponent = self._eval_point(node.right, mp.mpf(0))
                return self._power_point(base, exponent, node)
            left = self._eval_point(node.left, x)
            right = self._eval_point(node.right, x)
            if node.op == "+":
                return left + right
            if node.op == "-":
                return left - right
            if node.op == "*":
                return left * right
            if node.op == "/":
                return left / right
        if isinstance(node, Call):
            v = self._eval_point(node.arg, x)
            if node.name == "log" and v <= 0:
                raise DomainEvaluationError(
                    "log is only real-valued for strictly positive arguments",
                    self._position(node.arg),
                )
            return {
                "sin": mp.sin,
                "cos": mp.cos,
                "tan": mp.tan,
                "exp": mp.exp,
                "log": mp.log,
                "sqrt": mp.sqrt,
            }[node.name](v)
        raise ComputationFailure(f"unknown AST node {type(node).__name__}")

    def _power_point(self, base, exponent, node: Binary):
        if exponent == self.mp.floor(exponent):
            return base ** int(self.mp.floor(exponent))
        if base < 0:
            raise DomainEvaluationError(
                "non-integer power of a negative value", self._position(node)
            )
        return base ** exponent

    # -- helpers -----------------------------------------------------------
    def _num_iv(self, text: str):
        # Num text may carry a synthesised leading sign (derived exponents);
        # build via decimal mpf endpoints so 0.1-style literals are enclosed.
        value = self.mp.mpf(text)
        return [value, value]

    def _thin_value(self, interval):
        return self.mp.mpf(interval._mpi_[0])

    def _zero_tuple(self):
        return self.iv.mpf([0, 0])._mpi_[0]

    @staticmethod
    def _is_nan(value) -> bool:
        a, b = value._mpi_
        # mpmath signals nan with man=0, exp=0 special tuples; check via str.
        return mpmath.mpf(a) != mpmath.mpf(a)

    @staticmethod
    def _unwrap(node: Node) -> Node:
        while isinstance(node, Paren):
            node = node.inner
        return node

    def _position(self, node: Node) -> SourcePosition:
        """Find the nearest real source span for ``node``.

        Synthesised derivative nodes carry ``-1`` spans; descend to the first
        descendant that came from the user's source so the error still points
        at a meaningful character range.
        """
        real = self._unwrap(node)
        found = _find_real_span(real)
        if found is None:
            return SourcePosition(start=0, end=0, snippet="")
        start, end = found
        return SourcePosition(start=start, end=max(end, start), snippet="")
