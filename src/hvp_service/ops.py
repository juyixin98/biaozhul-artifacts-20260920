"""Operator set: forward rules, shape inference, and backward (VJP) rules.

Backward rules are expressed by *building new nodes in the same operator
set* (via ``BackwardCtx.builder``). This is what makes double-backward —
and therefore Hessian-vector products without an explicit Hessian —
possible: the gradient graph is itself differentiable by construction.

Convention: ``ctx.g`` is the node id of the upstream adjoint (same shape as
the op output), ``ctx.out`` the node id of this op's output, and each rule
returns one node id (or ``None`` for an exactly-zero contribution) per
input, in input order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from .errors import input_error

Array = np.ndarray
Shape = tuple[int, ...]


@dataclass
class BackwardCtx:
    builder: Any  # GraphBuilder (duck-typed to avoid a circular import)
    g: int
    out: int
    inputs: tuple[int, ...]
    params: dict[str, Any]


ForwardFn = Callable[[list[Array], dict[str, Any]], Array]
ShapeFn = Callable[[list[Shape], dict[str, Any]], Shape]
BackwardFn = Callable[[BackwardCtx], "list[int | None]"]
KinkFn = Callable[[list[Array], dict[str, Any], float], list[dict[str, Any]]]


@dataclass(frozen=True)
class Op:
    name: str
    arity: int
    forward: ForwardFn
    shape_fn: ShapeFn
    backward: BackwardFn
    kink_fn: KinkFn | None = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _as_float_array(value: Any) -> Array:
    try:
        return np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise input_error("const value must be numeric", error=str(exc)) from exc


def _sum_to(x: Array, shape: Shape) -> Array:
    """Reduce ``x`` to ``shape`` by summing broadcast axes (inverse of broadcast)."""
    while x.ndim > len(shape):
        x = x.sum(axis=0)
    for axis, dim in enumerate(shape):
        if dim == 1 and x.shape[axis] != 1:
            x = x.sum(axis=axis, keepdims=True)
    return x.reshape(shape)


def _broadcast_shape(shapes: list[Shape], params: dict[str, Any]) -> Shape:
    try:
        return tuple(np.broadcast_shapes(*shapes))
    except ValueError as exc:
        raise input_error("operand shapes are not broadcast-compatible", shapes=[list(s) for s in shapes]) from exc


def _same_shape(shapes: list[Shape], params: dict[str, Any]) -> Shape:
    return shapes[0]


def _param_shape(params: dict[str, Any], key: str = "shape") -> Shape:
    raw = params.get(key)
    if raw is None:
        raise input_error(f"missing required param '{key}'")
    try:
        shape = tuple(int(d) for d in raw)
    except (TypeError, ValueError) as exc:
        raise input_error(f"param '{key}' must be a list of integers", value=raw) from exc
    if any(d < 0 for d in shape):
        raise input_error(f"param '{key}' dimensions must be non-negative", value=raw)
    return shape


def _ub(ctx: BackwardCtx, g: int, target: Shape) -> int:
    """Un-broadcast adjoint ``g`` down to ``target`` shape (no-op if equal)."""
    if ctx.builder.shape_of(g) == tuple(target):
        return g
    return ctx.builder.add_node("sum_to", [g], {"shape": list(target)})


def _subgrad_value(ctx: BackwardCtx) -> float:
    return float(ctx.builder.build_options.get("subgradient", 0.0))


# ---------------------------------------------------------------------------
# shape functions
# ---------------------------------------------------------------------------


def _shape_const(shapes: list[Shape], params: dict[str, Any]) -> Shape:
    if "value" not in params:
        raise input_error("const node requires param 'value'")
    return tuple(_as_float_array(params["value"]).shape)


def _shape_input(shapes: list[Shape], params: dict[str, Any]) -> Shape:
    if not params.get("name"):
        raise input_error("input node requires param 'name'")
    return _param_shape(params)


def _shape_sum(shapes: list[Shape], params: dict[str, Any]) -> Shape:
    return ()


def _shape_dot(shapes: list[Shape], params: dict[str, Any]) -> Shape:
    a, b = shapes
    if len(a) != 1 or len(b) != 1 or a[0] != b[0]:
        raise input_error("dot requires two 1-D operands of equal length", shapes=[list(a), list(b)])
    return ()


def _shape_matmul(shapes: list[Shape], params: dict[str, Any]) -> Shape:
    a, b = shapes
    if len(a) != 2 or len(b) != 2 or a[1] != b[0]:
        raise input_error("matmul requires 2-D operands with matching inner dims", shapes=[list(a), list(b)])
    return (a[0], b[1])


def _shape_transpose(shapes: list[Shape], params: dict[str, Any]) -> Shape:
    (a,) = shapes
    if len(a) != 2:
        raise input_error("transpose requires a 2-D operand", shape=list(a))
    return (a[1], a[0])


def _shape_reshape(shapes: list[Shape], params: dict[str, Any]) -> Shape:
    (a,) = shapes
    target = _param_shape(params)
    if int(np.prod(a, dtype=np.int64)) != (int(np.prod(target, dtype=np.int64)) if target else 1):
        raise input_error("reshape must preserve element count", from_shape=list(a), to_shape=list(target))
    return target


def _shape_take(shapes: list[Shape], params: dict[str, Any]) -> Shape:
    (a,) = shapes
    if len(a) != 1:
        raise input_error("take requires a 1-D operand", shape=list(a))
    idx = params.get("index")
    if not isinstance(idx, int) or isinstance(idx, bool) or not (0 <= idx < a[0]):
        raise input_error("take param 'index' out of range", index=idx, length=a[0])
    return ()


def _shape_scatter(shapes: list[Shape], params: dict[str, Any]) -> Shape:
    scalar, ref = shapes
    if scalar != () or len(ref) != 1:
        raise input_error("scatter requires a scalar value and a 1-D reference", shapes=[list(scalar), list(ref)])
    idx = params.get("index")
    if not isinstance(idx, int) or isinstance(idx, bool) or not (0 <= idx < ref[0]):
        raise input_error("scatter param 'index' out of range", index=idx, length=ref[0])
    return ref


def _shape_sum_to(shapes: list[Shape], params: dict[str, Any]) -> Shape:
    (a,) = shapes
    target = _param_shape(params)
    if len(target) > len(a) or any(t != s and t != 1 for t, s in zip(target, a[len(a) - len(target):])):
        raise input_error("sum_to target is not a broadcast reduction of the input", from_shape=list(a), to_shape=list(target))
    return target


def _shape_broadcast_to(shapes: list[Shape], params: dict[str, Any]) -> Shape:
    (a,) = shapes
    target = _param_shape(params)
    try:
        np.broadcast_shapes(a, target)
    except ValueError as exc:
        raise input_error("broadcast_to target incompatible with input", from_shape=list(a), to_shape=list(target)) from exc
    if tuple(np.broadcast_shapes(a, target)) != target:
        raise input_error("broadcast_to target must be the broadcast of input and target", from_shape=list(a), to_shape=list(target))
    return target


# ---------------------------------------------------------------------------
# backward rules
# ---------------------------------------------------------------------------


def _bw_none(ctx: BackwardCtx) -> list[int | None]:
    return []


def _bw_add(ctx: BackwardCtx) -> list[int | None]:
    a, b = ctx.inputs
    B = ctx.builder
    return [_ub(ctx, ctx.g, B.shape_of(a)), _ub(ctx, ctx.g, B.shape_of(b))]


def _bw_sub(ctx: BackwardCtx) -> list[int | None]:
    a, b = ctx.inputs
    B = ctx.builder
    neg_g = B.add_node("neg", [ctx.g])
    return [_ub(ctx, ctx.g, B.shape_of(a)), _ub(ctx, neg_g, B.shape_of(b))]


def _bw_mul(ctx: BackwardCtx) -> list[int | None]:
    a, b = ctx.inputs
    B = ctx.builder
    da = B.add_node("mul", [ctx.g, b])
    db = B.add_node("mul", [ctx.g, a])
    return [_ub(ctx, da, B.shape_of(a)), _ub(ctx, db, B.shape_of(b))]


def _bw_div(ctx: BackwardCtx) -> list[int | None]:
    a, b = ctx.inputs
    B = ctx.builder
    da = B.add_node("div", [ctx.g, b])
    num = B.add_node("mul", [ctx.g, a])
    den = B.add_node("mul", [b, b])
    db = B.add_node("neg", [B.add_node("div", [num, den])])
    return [_ub(ctx, da, B.shape_of(a)), _ub(ctx, db, B.shape_of(b))]


def _bw_neg(ctx: BackwardCtx) -> list[int | None]:
    return [ctx.builder.add_node("neg", [ctx.g])]


def _bw_pow_const(ctx: BackwardCtx) -> list[int | None]:
    (a,) = ctx.inputs
    B = ctx.builder
    c = float(ctx.params["exponent"])
    c_node = B.add_node("const", [], {"value": c})
    p = B.add_node("pow_const", [a], {"exponent": c - 1.0})
    return [B.add_node("mul", [B.add_node("mul", [ctx.g, p]), c_node])]


def _bw_exp(ctx: BackwardCtx) -> list[int | None]:
    return [ctx.builder.add_node("mul", [ctx.g, ctx.out])]


def _bw_log(ctx: BackwardCtx) -> list[int | None]:
    (a,) = ctx.inputs
    return [ctx.builder.add_node("div", [ctx.g, a])]


def _bw_sin(ctx: BackwardCtx) -> list[int | None]:
    (a,) = ctx.inputs
    B = ctx.builder
    return [B.add_node("mul", [ctx.g, B.add_node("cos", [a])])]


def _bw_cos(ctx: BackwardCtx) -> list[int | None]:
    (a,) = ctx.inputs
    B = ctx.builder
    return [B.add_node("neg", [B.add_node("mul", [ctx.g, B.add_node("sin", [a])])])]


def _bw_tanh(ctx: BackwardCtx) -> list[int | None]:
    B = ctx.builder
    one = B.add_node("const", [], {"value": 1.0})
    sq = B.add_node("mul", [ctx.out, ctx.out])
    return [B.add_node("mul", [ctx.g, B.add_node("sub", [one, sq])])]


def _bw_abs(ctx: BackwardCtx) -> list[int | None]:
    (a,) = ctx.inputs
    B = ctx.builder
    s = B.add_node("abs_subgrad", [a], {"g0": _subgrad_value(ctx)})
    return [B.add_node("mul", [ctx.g, s])]


def _bw_relu(ctx: BackwardCtx) -> list[int | None]:
    (a,) = ctx.inputs
    B = ctx.builder
    s = B.add_node("relu_subgrad", [a], {"g0": _subgrad_value(ctx)})
    return [B.add_node("mul", [ctx.g, s])]


def _bw_zero(ctx: BackwardCtx) -> list[int | None]:
    """Exactly-zero derivative (piecewise-constant subgradient maps, zeros_like)."""
    return [None] * len(ctx.inputs)


def _bw_sum(ctx: BackwardCtx) -> list[int | None]:
    (a,) = ctx.inputs
    B = ctx.builder
    return [B.add_node("broadcast_to", [ctx.g], {"shape": list(B.shape_of(a))})]


def _bw_dot(ctx: BackwardCtx) -> list[int | None]:
    a, b = ctx.inputs
    B = ctx.builder
    return [B.add_node("mul", [ctx.g, b]), B.add_node("mul", [ctx.g, a])]


def _bw_matmul(ctx: BackwardCtx) -> list[int | None]:
    a, b = ctx.inputs
    B = ctx.builder
    da = B.add_node("matmul", [ctx.g, B.add_node("transpose", [b])])
    db = B.add_node("matmul", [B.add_node("transpose", [a]), ctx.g])
    return [da, db]


def _bw_transpose(ctx: BackwardCtx) -> list[int | None]:
    return [ctx.builder.add_node("transpose", [ctx.g])]


def _bw_reshape(ctx: BackwardCtx) -> list[int | None]:
    (a,) = ctx.inputs
    B = ctx.builder
    return [B.add_node("reshape", [ctx.g], {"shape": list(B.shape_of(a))})]


def _bw_take(ctx: BackwardCtx) -> list[int | None]:
    (a,) = ctx.inputs
    B = ctx.builder
    return [B.add_node("scatter", [ctx.g, a], {"index": int(ctx.params["index"])})]


def _bw_scatter(ctx: BackwardCtx) -> list[int | None]:
    # out = zeros_like(ref) with value placed at index; ref contributes nothing.
    B = ctx.builder
    return [B.add_node("take", [ctx.g], {"index": int(ctx.params["index"])}), None]


def _bw_sum_to(ctx: BackwardCtx) -> list[int | None]:
    (a,) = ctx.inputs
    B = ctx.builder
    return [B.add_node("broadcast_to", [ctx.g], {"shape": list(B.shape_of(a))})]


def _bw_broadcast_to(ctx: BackwardCtx) -> list[int | None]:
    (a,) = ctx.inputs
    B = ctx.builder
    return [B.add_node("sum_to", [ctx.g], {"shape": list(B.shape_of(a))})]


# ---------------------------------------------------------------------------
# kink detectors (non-smooth points)
# ---------------------------------------------------------------------------


def _kink_at_zero(args: list[Array], params: dict[str, Any], atol: float) -> list[dict[str, Any]]:
    x = args[0]
    hits = np.argwhere(np.abs(x) <= max(atol, 0.0))
    return [
        {"index": [int(i) for i in idx], "value": float(x[tuple(idx)])}
        for idx in hits
    ]


# ---------------------------------------------------------------------------
# forward rules
# ---------------------------------------------------------------------------


def _fw_input(args: list[Array], params: dict[str, Any]) -> Array:  # pragma: no cover
    raise input_error("input nodes are bound at evaluation time, not computed")


def _fw_const(args: list[Array], params: dict[str, Any]) -> Array:
    return _as_float_array(params["value"])


def _fw_sum_to(args: list[Array], params: dict[str, Any]) -> Array:
    return _sum_to(np.asarray(args[0], dtype=np.float64), _param_shape(params))


def _fw_broadcast_to(args: list[Array], params: dict[str, Any]) -> Array:
    return np.broadcast_to(args[0], _param_shape(params)).astype(np.float64)


def _fw_take(args: list[Array], params: dict[str, Any]) -> Array:
    return np.asarray(args[0][int(params["index"])], dtype=np.float64)


def _fw_scatter(args: list[Array], params: dict[str, Any]) -> Array:
    out = np.zeros_like(np.asarray(args[1], dtype=np.float64))
    out[int(params["index"])] = float(args[0])
    return out


def _fw_abs_subgrad(args: list[Array], params: dict[str, Any]) -> Array:
    x = args[0]
    g0 = float(params.get("g0", 0.0))
    return np.where(x > 0, 1.0, np.where(x < 0, -1.0, g0))


def _fw_relu_subgrad(args: list[Array], params: dict[str, Any]) -> Array:
    x = args[0]
    g0 = float(params.get("g0", 0.0))
    return np.where(x > 0, 1.0, np.where(x < 0, 0.0, g0))


def _fw_zeros_like(args: list[Array], params: dict[str, Any]) -> Array:
    return np.zeros_like(np.asarray(args[0], dtype=np.float64))


def _fw_dot(args: list[Array], params: dict[str, Any]) -> Array:
    return np.asarray(np.dot(args[0], args[1]), dtype=np.float64)


def _fw_pow_const(args: list[Array], params: dict[str, Any]) -> Array:
    return np.power(args[0], float(params["exponent"]))


def _fw_reshape(args: list[Array], params: dict[str, Any]) -> Array:
    return np.asarray(args[0], dtype=np.float64).reshape(_param_shape(params))


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

def _unary(fw: Callable[[Array], Array]) -> ForwardFn:
    return lambda args, params: fw(args[0])


def _binary(fw: Callable[[Array, Array], Array]) -> ForwardFn:
    return lambda args, params: fw(args[0], args[1])


OPS: dict[str, Op] = {}


def _register(op: Op) -> None:
    OPS[op.name] = op


_register(Op("input", 0, _fw_input, _shape_input, _bw_none))
_register(Op("const", 0, _fw_const, _shape_const, _bw_none))
_register(Op("add", 2, _binary(lambda a, b: a + b), _broadcast_shape, _bw_add))
_register(Op("sub", 2, _binary(lambda a, b: a - b), _broadcast_shape, _bw_sub))
_register(Op("mul", 2, _binary(lambda a, b: a * b), _broadcast_shape, _bw_mul))
_register(Op("div", 2, _binary(lambda a, b: a / b), _broadcast_shape, _bw_div))
_register(Op("neg", 1, _unary(lambda a: -a), _same_shape, _bw_neg))
_register(Op("pow_const", 1, _fw_pow_const, _same_shape, _bw_pow_const))
_register(Op("exp", 1, _unary(np.exp), _same_shape, _bw_exp))
_register(Op("log", 1, _unary(np.log), _same_shape, _bw_log))
_register(Op("sin", 1, _unary(np.sin), _same_shape, _bw_sin))
_register(Op("cos", 1, _unary(np.cos), _same_shape, _bw_cos))
_register(Op("tanh", 1, _unary(np.tanh), _same_shape, _bw_tanh))
_register(Op("abs", 1, _unary(np.abs), _same_shape, _bw_abs, kink_fn=_kink_at_zero))
_register(Op("relu", 1, _unary(lambda a: np.maximum(a, 0.0)), _same_shape, _bw_relu, kink_fn=_kink_at_zero))
_register(Op("abs_subgrad", 1, _fw_abs_subgrad, _same_shape, _bw_zero))
_register(Op("relu_subgrad", 1, _fw_relu_subgrad, _same_shape, _bw_zero))
_register(Op("zeros_like", 1, _fw_zeros_like, _same_shape, _bw_zero))
_register(Op("sum", 1, _unary(lambda a: np.asarray(np.sum(a), dtype=np.float64)), _shape_sum, _bw_sum))
_register(Op("dot", 2, _fw_dot, _shape_dot, _bw_dot))
_register(Op("matmul", 2, _binary(lambda a, b: a @ b), _shape_matmul, _bw_matmul))
_register(Op("transpose", 1, _unary(lambda a: a.T), _shape_transpose, _bw_transpose))
_register(Op("reshape", 1, _fw_reshape, _shape_reshape, _bw_reshape))
_register(Op("take", 1, _fw_take, _shape_take, _bw_take))
_register(Op("scatter", 2, _fw_scatter, _shape_scatter, _bw_scatter))
_register(Op("sum_to", 1, _fw_sum_to, _shape_sum_to, _bw_sum_to))
_register(Op("broadcast_to", 1, _fw_broadcast_to, _shape_broadcast_to, _bw_broadcast_to))
