"""Operator catalogue: forward rules, kink detection, local first
derivatives (symbolic) and tangent (JVP) rules.

The restricted differentiable expression language supports, with the usual
semantics:

    unary   neg sin cos exp log sqrt abs relu sign
    binary  add sub mul div max min

``abs``, ``relu``, ``sign``, ``max`` and ``min`` are piecewise smooth. Their
non-differentiable points are detected *exactly* (``x == 0`` / ``x == y``)
from primal values; behaviour there is governed by ``NonsmoothConfig``:
either reject with :class:`~hvpsvc.errors.NonSmoothError` or use a designated
subderivative.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Sequence

from .errors import InputError, NonSmoothError


@dataclass(frozen=True)
class OpSpec:
    name: str
    arity: int
    forward: Callable[[Sequence[float]], float]
    nonsmooth: bool = False
    #: returns True when the inputs sit exactly on the kink
    at_kink: Callable[[Sequence[float]], bool] | None = None


# ---------------------------------------------------------------------------
# Forward rules
# ---------------------------------------------------------------------------

def _neg(a: Sequence[float]) -> float:
    return -a[0]


def _sin(a: Sequence[float]) -> float:
    return math.sin(a[0])


def _cos(a: Sequence[float]) -> float:
    return math.cos(a[0])


def _exp(a: Sequence[float]) -> float:
    return math.exp(a[0])


def _log(a: Sequence[float]) -> float:
    if a[0] <= 0.0:
        # explicit domain error instead of -inf / nan
        raise ValueError(f"log domain error: x={a[0]}")
    return math.log(a[0])


def _sqrt(a: Sequence[float]) -> float:
    if a[0] < 0.0:
        raise ValueError(f"sqrt domain error: x={a[0]}")
    return math.sqrt(a[0])


def _abs(a: Sequence[float]) -> float:
    return abs(a[0])


def _relu(a: Sequence[float]) -> float:
    return a[0] if a[0] > 0.0 else 0.0


def _sign(a: Sequence[float]) -> float:
    return 1.0 if a[0] > 0.0 else (-1.0 if a[0] < 0.0 else 0.0)


def _add(a: Sequence[float]) -> float:
    return a[0] + a[1]


def _sub(a: Sequence[float]) -> float:
    return a[0] - a[1]


def _mul(a: Sequence[float]) -> float:
    return a[0] * a[1]


def _div(a: Sequence[float]) -> float:
    if a[1] == 0.0:
        raise ZeroDivisionError(f"division by zero: {a[0]}/{a[1]}")
    return a[0] / a[1]


def _max(a: Sequence[float]) -> float:
    return max(a[0], a[1])


def _min(a: Sequence[float]) -> float:
    return min(a[0], a[1])


def _is_zero(a: Sequence[float]) -> bool:
    return a[0] == 0.0


def _is_tied(a: Sequence[float]) -> bool:
    return a[0] == a[1]


_OPS: dict[str, OpSpec] = {}


def _register(spec: OpSpec) -> OpSpec:
    _OPS[spec.name] = spec
    return spec


_REGISTERED: tuple[OpSpec, ...] = (
    _register(OpSpec("neg", 1, _neg)),
    _register(OpSpec("sin", 1, _sin)),
    _register(OpSpec("cos", 1, _cos)),
    _register(OpSpec("exp", 1, _exp)),
    _register(OpSpec("log", 1, _log)),
    _register(OpSpec("sqrt", 1, _sqrt)),
    _register(OpSpec("abs", 1, _abs, nonsmooth=True, at_kink=_is_zero)),
    _register(OpSpec("relu", 1, _relu, nonsmooth=True, at_kink=_is_zero)),
    _register(OpSpec("sign", 1, _sign, nonsmooth=True, at_kink=_is_zero)),
    _register(OpSpec("add", 2, _add)),
    _register(OpSpec("sub", 2, _sub)),
    _register(OpSpec("mul", 2, _mul)),
    _register(OpSpec("div", 2, _div)),
    _register(OpSpec("max", 2, _max, nonsmooth=True, at_kink=_is_tied)),
    _register(OpSpec("min", 2, _min, nonsmooth=True, at_kink=_is_tied)),
)


def get(name: str | None) -> OpSpec:
    if name not in _OPS:
        raise InputError(f"unknown op {name!r}", detail={
            "op": name,
            "known_ops": sorted(_OPS),
        })
    return _OPS[name]  # type: ignore[index]


def op_names() -> list[str]:
    return sorted(_OPS)


# ---------------------------------------------------------------------------
# Subderivative selection at kinks
# ---------------------------------------------------------------------------

def unary_slope(op: str, values: Sequence[float], subgradient: float,
                node_id: str) -> float:
    """Designated first derivative of a unary piecewise-smooth op at a kink.

    ``subgradient`` s in [0, 1] is mapped to the op's subderivative range:
    relu -> [0, 1]; abs/sign -> [-1, 1] via 2s-1.
    """
    if op == "relu":
        return float(subgradient)
    if op in ("abs", "sign"):
        return 2.0 * float(subgradient) - 1.0
    raise NonSmoothError(
        f"node {node_id!r}: non-differentiable point of {op!r} at x={values[0]}",
        detail={"node": node_id, "op": op, "inputs": list(values)},
    )


def binary_slopes(op: str, values: Sequence[float], subgradient: float,
                  node_id: str) -> tuple[float, float]:
    """Designated partials of a binary piecewise-smooth op at a tie.

    max/min at x == y take convex weights (s, 1-s), s in [0, 1].
    """
    if op in ("max", "min"):
        return float(subgradient), 1.0 - float(subgradient)
    raise NonSmoothError(
        f"node {node_id!r}: non-differentiable point of {op!r} at {list(values)}",
        detail={"node": node_id, "op": op, "inputs": list(values)},
    )
