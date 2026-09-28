"""Numerical validation against *independent* references.

Nothing in this module reuses the autodiff core. Two independent oracles are
provided:

1. :class:`MpmathReference` - a from-scratch interpreter of the raw
   expression spec written on top of ``mpmath`` at 50 decimal digits. It
   builds the **explicit, dense Hessian** and gradient by high-precision
   numerical differentiation (``mpmath.diff``). This is the reference answer
   for HVP and gradient checks; it costs O(n^2) and exists only for small
   verification dimensions.

2. Central finite differences (float64) of the *primal value alone* - an
   independent gradient estimate that exercises none of the reverse-mode
   machinery.

Both return structured component-wise diagnostics (abs/rel error, residual
norm) plus a verdict and reason, so a failing test can be replayed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import mpmath
import numpy as np

from .errors import InputError
from .tensor import InputLayout

mpmath.mp.dps = 50
_DTYPE = np.float64


@dataclass
class CheckResult:
    name: str
    passed: bool
    reason: str
    max_abs_error: float
    max_rel_error: float
    tolerance: float
    expected: list[float] = field(default_factory=list)
    actual: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "reason": self.reason,
            "max_abs_error": self.max_abs_error,
            "max_rel_error": self.max_rel_error,
            "tolerance": self.tolerance,
            "expected": self.expected,
            "actual": self.actual,
        }


def _componentwise_errors(expected: np.ndarray, actual: np.ndarray,
                          scale_floor: float = 1e-12) -> tuple[float, float]:
    diff = np.abs(expected - actual)
    scale = np.maximum(np.abs(expected), scale_floor)
    return float(diff.max(initial=0.0)), float((diff / scale).max(initial=0.0))


# ---------------------------------------------------------------------------
# Independent high-precision reference (does NOT import the autodiff core)
# ---------------------------------------------------------------------------

class MpmathReference:
    """Re-interprets a raw expression spec with arbitrary-precision mpmath."""

    _UNARY = {
        "neg": lambda a: -a,
        "sin": mpmath.sin,
        "cos": mpmath.cos,
        "exp": mpmath.exp,
        "log": mpmath.log,
        "sqrt": mpmath.sqrt,
        "abs": mpmath.fabs,
        "relu": lambda a: a if a > 0 else mpmath.mpf(0),
        "sign": lambda a: mpmath.sign(a),
    }
    _BINARY = {
        "add": lambda a, b: a + b,
        "sub": lambda a, b: a - b,
        "mul": lambda a, b: a * b,
        "div": lambda a, b: a / b,
        "max": mpmath.fmax if hasattr(mpmath, "fmax") else lambda a, b: max(a, b),
        "min": mpmath.fmin if hasattr(mpmath, "fmin") else lambda a, b: min(a, b),
    }

    def __init__(self, spec: dict[str, Any], layout: InputLayout) -> None:
        self.layout = layout
        self.n = layout.total_size
        if self.n > 64:
            raise InputError(
                "high-precision reference is limited to 64 input components "
                f"(got {self.n}); use it for verification only",
                detail={"size": self.n, "limit": 64})
        self._var_index: dict[str, int] = {}
        offset = 0
        for v in layout.variables:
            self._var_index[v.name] = offset
            offset += v.size
        self._ops: list[tuple[str, str, tuple, Any]] = []
        self._compile(spec)

    def _compile(self, spec: dict[str, Any]) -> None:
        """Own minimal compiler: list of (user_id, kind, args, payload)."""
        symbols: dict[str, str] = {}  # id -> ('var', flat idx) | ('node', i)
        for name, off in self._var_index.items():
            lay = self.layout.layout(name)
            for idx in range(lay.size):
                # variable components addressed directly only via get nodes;
                # scalar variables are usable bare
                if lay.is_scalar:
                    symbols[name] = ("var", off)
        nodes = spec.get("nodes")
        if not isinstance(nodes, list):
            raise InputError("reference: nodes must be a list")
        for ns in nodes:
            nid, op = ns["id"], ns["op"]
            args = ns.get("args", [])
            if op == "get":
                symbols[nid] = ("var", self._var_index[args[0]] + int(ns["index"]))
                continue
            if op == "scalar":
                self._ops.append((nid, "const", (), mpmath.mpf(str(ns["value"]))))
                symbols[nid] = ("node", len(self._ops) - 1)
                continue

            def resolve(ref: str) -> tuple:
                if ref not in symbols:
                    raise InputError(f"reference: unknown arg {ref!r}")
                return symbols[ref]

            resolved = tuple(resolve(a) for a in args)
            self._ops.append((nid, op, resolved, None))
            symbols[nid] = ("node", len(self._ops) - 1)
        out = spec.get("output")
        if out not in symbols:
            raise InputError("reference: unknown output")
        self._output = symbols[out]

    def _eval_flat(self, x: list[mpmath.mpf]) -> mpmath.mpf:
        env: list[mpmath.mpf] = []
        for _, kind, args, payload in self._ops:
            if kind == "const":
                env.append(payload)
                continue

            def val(sym: tuple) -> mpmath.mpf:
                kind2, ref = sym
                return x[ref] if kind2 == "var" else env[ref]

            if kind in self._UNARY:
                env.append(self._UNARY[kind](val(args[0])))
            elif kind in self._BINARY:
                env.append(self._BINARY[kind](val(args[0]), val(args[1])))
            else:
                raise InputError(f"reference: unsupported op {kind!r}")
        kind2, ref = self._output
        return x[ref] if kind2 == "var" else env[ref]

    def value_gradient_hessian(
        self, x_flat: np.ndarray
    ) -> tuple[float, np.ndarray, np.ndarray]:
        """Explicit (value, gradient, dense Hessian) at high precision.

        Gradient entries are first derivatives; diagonal Hessian entries are
        second derivatives; off-diagonal entries are nested first
        derivatives (d/dx_j of d/dx_i) - all via mpmath numerical
        differentiation, independently of the service's AD rules.
        """
        x0 = [mpmath.mpf(repr(float(v))) for v in np.asarray(x_flat, dtype=_DTYPE)]

        def f_vec(xv: list) -> mpmath.mpf:
            return self._eval_flat(list(xv))

        def f_component(base: list, i: int, t) -> mpmath.mpf:
            xv = list(base)
            xv[i] = t
            return f_vec(xv)

        f0 = f_vec(x0)
        grad = np.zeros(self.n, dtype=_DTYPE)
        hess = np.zeros((self.n, self.n), dtype=_DTYPE)
        for i in range(self.n):
            grad[i] = float(mpmath.diff(
                lambda t, i=i: f_component(x0, i, t), x0[i], 1))
            hess[i, i] = float(mpmath.diff(
                lambda t, i=i: f_component(x0, i, t), x0[i], 2))
            for j in range(i + 1, self.n):
                # d/dx_j (d f / d x_i): first differentiate in i with j held
                # at t, then differentiate the resulting scalar in j.
                def dfdi(t, i=i, j=j):
                    base = list(x0)
                    base[j] = t
                    return mpmath.diff(
                        lambda s, base=base, i=i: f_component(base, i, s),
                        x0[i], 1)

                hij = float(mpmath.diff(dfdi, x0[j], 1))
                hess[i, j] = hij
                hess[j, i] = hij
        hess = 0.5 * (hess + hess.T)  # symmetrize residual rounding
        return float(f0), grad, hess


# ---------------------------------------------------------------------------
# Finite-difference gradient oracle (primal value only, independent of AD)
# ---------------------------------------------------------------------------

def finite_difference_gradient(
    primal_fn: Callable[[np.ndarray], float],
    x: np.ndarray,
    *,
    step: float = 1e-6,
) -> np.ndarray:
    """Central differences; relative step per component."""
    x = np.asarray(x, dtype=_DTYPE)
    grad = np.zeros_like(x)
    for i in range(x.size):
        h = step * max(1.0, abs(float(x[i])))
        xp = x.copy()
        xm = x.copy()
        xp[i] += h
        xm[i] -= h
        grad[i] = (primal_fn(xp) - primal_fn(xm)) / (2.0 * h)
    return grad


def check_gradient_against_ref(expected: np.ndarray, actual: np.ndarray, *,
                               tol: float = 1e-8,
                               name: str = "gradient_vs_mpmath") -> CheckResult:
    expected = np.asarray(expected, dtype=_DTYPE).reshape(-1)
    actual = np.asarray(actual, dtype=_DTYPE).reshape(-1)
    abs_err, rel_err = _componentwise_errors(expected, actual)
    passed = rel_err <= tol
    return CheckResult(
        name=name,
        passed=passed,
        reason=("within tolerance" if passed
                else f"rel error {rel_err:.3e} exceeds tolerance {tol:.0e}"),
        max_abs_error=abs_err,
        max_rel_error=rel_err,
        tolerance=tol,
        expected=expected.tolist(),
        actual=actual.tolist(),
    )


def check_vector_against_ref(expected: np.ndarray, actual: np.ndarray, *,
                             tol: float = 1e-8,
                             name: str = "hvp_vs_explicit_hessian",
                             zero_tol: float = 1e-11) -> CheckResult:
    """Compare HVP to the dense H @ v from the high-precision reference."""
    expected = np.asarray(expected, dtype=_DTYPE).reshape(-1)
    actual = np.asarray(actual, dtype=_DTYPE).reshape(-1)
    abs_err, rel_err = _componentwise_errors(expected, actual)
    if np.all(np.abs(expected) <= zero_tol):
        passed = abs_err <= tol
        reason = ("zero-direction exact" if passed
                  else f"expected ~zero but abs error {abs_err:.3e}")
    else:
        passed = rel_err <= tol
        reason = ("within tolerance" if passed
                  else f"rel error {rel_err:.3e} exceeds tolerance {tol:.0e}")
    return CheckResult(
        name=name,
        passed=passed,
        reason=reason,
        max_abs_error=abs_err,
        max_rel_error=rel_err,
        tolerance=tol,
        expected=expected.tolist(),
        actual=actual.tolist(),
    )
