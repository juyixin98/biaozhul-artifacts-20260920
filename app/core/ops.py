"""Operator library: shape inference, compute costs and VJP rules.

Every op knows three things, kept in one place so the planner, the executor
and the tests share a single contract:

* ``infer_shape``            -- symbolic output shape + input validation
* ``compute_cost``           -- proxy FLOPs of one forward evaluation
* ``scratch_cost``           -- temporary elements live only while the op runs
* ``needs_activations``      -- which *input* activations its VJP requires
                                (the node's own output is handled separately)

The stochastic op ``dropout`` and the side-effecting op ``external`` are what
make the replay/side-effect contracts (phase 2) testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

from .errors import InvalidInputError

INPUT = "input"
PARAMETER = "parameter"
LINEAR = "linear"
ADD = "add"
MUL = "mul"
RELU = "relu"
DROPOUT = "dropout"
EXTERNAL = "external"
REDUCE_SUM = "reduce_sum"

ALL_OPS = frozenset(
    {INPUT, PARAMETER, LINEAR, ADD, MUL, RELU, DROPOUT, EXTERNAL, REDUCE_SUM}
)
ROOT_OPS = frozenset({INPUT, PARAMETER})


@dataclass(frozen=True)
class OpSpec:
    arity: Tuple[int, int]  # inclusive (min, max) number of inputs
    stochastic: bool = False
    side_effect: bool = False
    needs_output_for_vjp: bool = False


SPECS: Dict[str, OpSpec] = {
    INPUT: OpSpec(arity=(0, 0)),
    PARAMETER: OpSpec(arity=(0, 0)),
    LINEAR: OpSpec(arity=(2, 3)),
    ADD: OpSpec(arity=(2, 2)),
    MUL: OpSpec(arity=(2, 2)),
    RELU: OpSpec(arity=(1, 1)),
    DROPOUT: OpSpec(arity=(1, 1), stochastic=True),
    # Identity on the numerical side; its only job is an external side effect.
    EXTERNAL: OpSpec(arity=(1, 1), side_effect=True),
    REDUCE_SUM: OpSpec(arity=(1, 1)),
}


# --------------------------------------------------------------------------
# Shape inference
# --------------------------------------------------------------------------


def infer_shape(op: str, params: Dict[str, Any], input_shapes: Sequence[Tuple[int, ...]]):
    if op == INPUT or op == PARAMETER:
        shape = params.get("shape")
        if not isinstance(shape, list) or not shape:
            raise InvalidInputError(
                f"{op} node requires a non-empty 'shape' list",
                code="E_OP_PARAM_MISSING",
                context={"op": op, "param": "shape"},
            )
        return tuple(int(d) for d in shape)
    if op == LINEAR:
        _check_arity(op, input_shapes, 2, 3)
        x, w = input_shapes[0], input_shapes[1]
        if len(x) != 2 or len(w) != 2 or x[1] != w[0]:
            raise InvalidInputError(
                "linear requires x (b,m) and w (m,n) with matching m",
                code="E_SHAPE_MISMATCH",
                context={"x": list(x), "w": list(w)},
            )
        if len(input_shapes) == 3:
            b = input_shapes[2]
            if b != (w[1],):
                raise InvalidInputError(
                    "linear bias must have shape (n,)",
                    code="E_SHAPE_MISMATCH",
                    context={"bias": list(b), "expected_n": w[1]},
                )
        return (x[0], w[1])
    if op in (ADD, MUL):
        _check_arity(op, input_shapes, 2, 2)
        if input_shapes[0] != input_shapes[1]:
            raise InvalidInputError(
                f"{op} requires equal input shapes",
                code="E_SHAPE_MISMATCH",
                context={"shapes": [list(s) for s in input_shapes]},
            )
        return input_shapes[0]
    if op in (RELU, DROPOUT, EXTERNAL):
        _check_arity(op, input_shapes, 1, 1)
        return input_shapes[0]
    if op == REDUCE_SUM:
        _check_arity(op, input_shapes, 1, 1)
        return (1,)
    raise InvalidInputError(
        f"unknown op {op!r}", code="E_OP_UNKNOWN", context={"op": op}
    )


def validate_params(op: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise/validate node params, returning the normalised copy."""

    norm = dict(params or {})
    if op in ROOT_OPS:
        shape = norm.get("shape")
        if not isinstance(shape, list) or not shape:
            raise InvalidInputError(
                f"{op} node requires a non-empty 'shape' list",
                code="E_OP_PARAM_MISSING",
                context={"op": op, "param": "shape"},
            )
        dims: List[int] = []
        for d in shape:
            if not isinstance(d, int) or isinstance(d, bool) or d <= 0:
                raise InvalidInputError(
                    "shape dimensions must be positive integers",
                    code="E_TENSOR_SHAPE_INVALID",
                    context={"op": op, "shape": shape},
                )
            dims.append(d)
        norm["shape"] = dims
    elif op == DROPOUT:
        p = norm.get("p", 0.0)
        if not isinstance(p, (int, float)) or isinstance(p, bool):
            raise InvalidInputError(
                "dropout 'p' must be a number in [0, 1)",
                code="E_OP_PARAM_INVALID",
                context={"op": op, "p": p},
            )
        if not 0.0 <= float(p) < 1.0:
            raise InvalidInputError(
                "dropout 'p' must be in [0, 1)",
                code="E_OP_PARAM_INVALID",
                context={"op": op, "p": p},
            )
        norm["p"] = float(p)
    elif op == LINEAR:
        norm.setdefault("use_bias", len(norm.get("_bias_shape", ())) > 0)
    return norm


def _check_arity(op: str, shapes: Sequence[Tuple[int, ...]], lo: int, hi: int) -> None:
    if not lo <= len(shapes) <= hi:
        raise InvalidInputError(
            f"{op} expects {lo}..{hi} inputs, got {len(shapes)}",
            code="E_OP_ARITY",
            context={"op": op, "got": len(shapes)},
        )


# --------------------------------------------------------------------------
# Costs (all in float elements / proxy flops -- see tensor.py)
# --------------------------------------------------------------------------


def numel(shape: Tuple[int, ...]) -> int:
    n = 1
    for d in shape:
        n *= d
    return n


def output_size(op: str, out_shape: Tuple[int, ...]) -> int:
    return numel(out_shape)


def compute_cost(op: str, in_shapes: Sequence[Tuple[int, ...]], out_shape) -> int:
    """Proxy arithmetic cost of one evaluation (extra-recompute counts these)."""

    if op == LINEAR:
        b, m = in_shapes[0]
        n = in_shapes[1][1]
        cost = 2 * b * m * n
        if len(in_shapes) == 3:
            cost += 2 * b * n
        return int(cost)
    if op == DROPOUT:
        # Draw + scale both touch the elements.
        return 2 * numel(out_shape)
    if op in (ADD, MUL, RELU, EXTERNAL, REDUCE_SUM):
        return numel(in_shapes[0]) if in_shapes else numel(out_shape)
    return 0  # roots: supplied, not computed


def scratch_cost(op: str, in_shapes, out_shape) -> int:
    """Op-private temporary elements, allocated while the op runs."""

    if op == LINEAR:
        # Backward builds one output-sized product at a time.
        return numel(out_shape)
    if op == DROPOUT:
        # The Bernoulli mask is a scratch buffer during recompute/backward.
        return numel(out_shape)
    if op == REDUCE_SUM:
        return numel(in_shapes[0])
    return 0


def backward_cost(op: str, in_shapes, out_shape) -> int:
    """Proxy arithmetic cost of one VJP evaluation."""

    if op == LINEAR:
        b, m = in_shapes[0]
        n = in_shapes[1][1]
        # grad_x: b*n*m, grad_w: b*m*n (+ bias reduction)
        cost = 2 * b * m * n
        if len(in_shapes) == 3:
            cost += b * n
        return int(cost)
    if op == DROPOUT:
        return numel(out_shape)
    if op in (ADD, MUL, RELU, EXTERNAL):
        return numel(out_shape)
    if op == REDUCE_SUM:
        # broadcast back to input shape
        return numel(in_shapes[0])
    return 0


def needs_input_activations(op: str) -> Tuple[int, ...]:
    """Input positions whose activations the VJP must have available."""

    if op == LINEAR:
        # x and w; a bias value is not needed to produce grad_x/grad_w.
        return (0, 1)
    if op == MUL:
        return (0, 1)
    if op == RELU:
        return (0,)
    # dropout gets its mask from RNG replay; add/external/sum need none.
    return ()


# --------------------------------------------------------------------------
# Numerical forward / backward (used by the executor)
# --------------------------------------------------------------------------


def forward(op: str, params: Dict[str, Any], inputs: Sequence[np.ndarray], rng,
            node_id: str, *, emit: bool,
            forced_mask: "np.ndarray | None" = None
            ) -> Tuple[np.ndarray, Any]:
    """Run the op. Returns (output, auxiliary) -- aux is ``None`` or mask."""

    if op == LINEAR:
        y = inputs[0] @ inputs[1]
        if len(inputs) == 3:
            y = y + inputs[2]
        return y, None
    if op == ADD:
        return inputs[0] + inputs[1], None
    if op == MUL:
        return inputs[0] * inputs[1], None
    if op == RELU:
        return np.maximum(inputs[0], 0.0), None
    if op == DROPOUT:
        if forced_mask is not None:
            mask = forced_mask
        else:
            mask = rng.dropout_mask(node_id, inputs[0].shape, params["p"])
        return inputs[0] * mask, mask
    if op == EXTERNAL:
        if emit:
            # The ONLY place a non-idempotent external side effect is allowed.
            rng.note_external_emit()
        return inputs[0].copy(), None
    if op == REDUCE_SUM:
        return inputs[0].sum(keepdims=True), None
    raise InvalidInputError(
        f"cannot evaluate root/unknown op {op!r}",
        code="E_OP_UNKNOWN",
        context={"op": op},
    )


def backward(op: str, params: Dict[str, Any],
             inputs: Sequence["np.ndarray | None"], aux: Any, grad_out: np.ndarray,
             mask_for_dropout: "np.ndarray | None" = None,
             input_shapes: "Sequence[tuple] | None" = None
             ) -> List[np.ndarray]:
    """VJP: return one gradient array per forward input (roots excluded).

    ``inputs`` contains actual arrays only at positions listed by
    :func:`needs_input_activations`; other positions are ``None``.  Shape-only
    needs (e.g. reduce_sum broadcasting) use ``input_shapes``.
    """

    if op == LINEAR:
        x, w = inputs[0], inputs[1]
        grads = [grad_out @ w.T, x.T @ grad_out]
        if input_shapes is not None and len(input_shapes) == 3:
            grads.append(grad_out.sum(axis=0))
        return grads
    if op == ADD:
        return [grad_out, grad_out]
    if op == MUL:
        return [grad_out * inputs[1], grad_out * inputs[0]]
    if op == RELU:
        return [grad_out * (inputs[0] > 0)]
    if op == DROPOUT:
        if mask_for_dropout is None:
            raise InvalidInputError(
                "dropout backward requires a replayed mask",
                code="E_RNG_MASK_MISSING",
                context={"op": op},
            )
        return [grad_out * mask_for_dropout]
    if op == EXTERNAL:
        return [grad_out]
    if op == REDUCE_SUM:
        if input_shapes is None:
            raise InvalidInputError(
                "reduce_sum backward requires input_shapes",
                code="E_OP_SHAPE_MISSING",
                context={"op": op},
            )
        return [
            np.broadcast_to(
                grad_out.reshape(()), input_shapes[0]
            ).astype(np.float64, copy=True)
        ]
    raise InvalidInputError(
        f"cannot differentiate op {op!r}",
        code="E_OP_UNDIFFERENTIABLE",
        context={"op": op},
    )
