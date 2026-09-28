"""Op registry: per-op contracts the planner and executor share.

Each op declares, in one place:

* arity and dtype/shape inference (``infer``),
* alias rules -- an output that is a *view* of an input (reshape/transpose)
  unions its alias class and lifetime with that input,
* scratch workspace bytes, live only during the op's own wave,
* the NumPy kernel used by the executor.

The set is deliberately small but covers elementwise, contraction, reduction,
and view (aliasing) ops.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from .errors import GraphValidationError, InputValidationError
from .tensor import (
    DEFAULT_ALIGNMENT,
    Shape,
    TensorType,
    align_up,
)

KernelFn = Callable[
    [list[np.ndarray], dict[str, Any], "np.ndarray | None", list["np.ndarray"]],
    list[np.ndarray],
]
InferFn = Callable[
    [list[TensorType], list[Shape], dict[str, Any]],
    tuple[list[TensorType], list[Shape]],
]
WorkspaceFn = Callable[[list[TensorType], list[Shape], dict[str, Any], int], int]


@dataclass(frozen=True)
class OpSpec:
    name: str
    ninputs: int
    noutputs: int
    infer: InferFn
    kernel: KernelFn
    workspace_fn: WorkspaceFn
    # alias[output_index] = input_index when the output is a view of an input
    alias: tuple[int | None, ...]
    # When True the op may produce a shape different from the static plan once
    # concrete run attrs/feeds are known (triggers capacity re-check).
    shape_sensitive: bool = False


# --------------------------------------------------------------------------- #
# Attribute helpers
# --------------------------------------------------------------------------- #


def _attr_int(attrs: dict[str, Any], key: str) -> int:
    if key not in attrs:
        raise InputValidationError(f"missing required attribute {key!r}", key=key)
    value = attrs[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise InputValidationError(
            f"attribute {key!r} must be an int", key=key, value=repr(value)
        )
    return value


def _attr_tuple_int(attrs: dict[str, Any], key: str) -> tuple[int, ...]:
    if key not in attrs:
        raise InputValidationError(f"missing required attribute {key!r}", key=key)
    value = attrs[key]
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(d, int) and not isinstance(d, bool) for d in value
    ):
        raise InputValidationError(
            f"attribute {key!r} must be a list/tuple of ints",
            key=key,
            value=repr(value),
        )
    return tuple(int(d) for d in value)


def _same_float(types: list[TensorType], opname: str) -> TensorType:
    dtype = types[0].dtype
    if not dtype.startswith(("float",)):
        raise GraphValidationError(f"{opname} expects floating operands", dtype=dtype)
    for t in types[1:]:
        if t != types[0]:
            raise GraphValidationError(
                f"{opname} operands must share dtype",
                dtypes=[t.dtype for t in types],
            )
    return types[0]


# --------------------------------------------------------------------------- #
# Shape inference
# --------------------------------------------------------------------------- #


def _infer_unary(types, shapes, attrs):
    _same_float(types, "unary elementwise")
    return [types[0]], [shapes[0]]


def _infer_binary(types, shapes, attrs):
    t = _same_float(types, "binary elementwise")
    if shapes[0] != shapes[1]:
        raise GraphValidationError(
            "binary elementwise operands must have equal shapes "
            "(broadcasting is not supported in the restricted runtime)",
            shapes=[str(shapes[0]), str(shapes[1])],
        )
    return [t], [shapes[0]]


def _infer_matmul(types, shapes, attrs):
    t = _same_float(types, "matmul")
    a, b = shapes
    if a.rank != 2 or b.rank != 2:
        raise GraphValidationError(
            "matmul supports rank-2 operands only",
            ranks=[a.rank, b.rank],
        )
    if a.dims[1] != b.dims[0]:
        raise GraphValidationError(
            "matmul inner dims must agree",
            a_shape=str(a),
            b_shape=str(b),
        )
    m, _k = a.dims
    _k2, n = b.dims
    return [t], [Shape(m, n)]


def _infer_reshape(types, shapes, attrs):
    target = _attr_tuple_int(attrs, "shape")
    out = Shape(*target)
    if out.numel != shapes[0].numel:
        raise GraphValidationError(
            "reshape cannot change element count",
            input_numel=shapes[0].numel,
            output_numel=out.numel,
        )
    return [TensorType(types[0].dtype, out.rank)], [out]


def _infer_transpose(types, shapes, attrs):
    rank = shapes[0].rank
    if "perm" in attrs:
        perm = _attr_tuple_int(attrs, "perm")
        if sorted(perm) != list(range(rank)):
            raise GraphValidationError(
                "transpose perm must be a permutation of input dims",
                perm=list(perm),
                rank=rank,
            )
    else:
        perm = tuple(range(rank - 1, -1, -1))
    out = Shape(*(shapes[0].dims[i] for i in perm))
    return [types[0]], [out]


def _infer_reduce_sum(types, shapes, attrs):
    t = _same_float(types, "reduce_sum")
    axis = _attr_int(attrs, "axis")
    keep = bool(attrs.get("keepdims", False))
    rank = shapes[0].rank
    if not -rank <= axis < rank:
        raise GraphValidationError(
            "reduce_sum axis out of range", axis=axis, rank=rank
        )
    norm = axis % rank
    if keep:
        dims = list(shapes[0].dims)
        dims[norm] = 1
        out_shape = Shape(*dims)
    else:
        dims = list(shapes[0].dims)
        dims.pop(norm)
        out_shape = Shape(*dims) if dims else Shape(1)
    return [TensorType(t.dtype, out_shape.rank)], [out_shape]


# --------------------------------------------------------------------------- #
# Workspace sizing (aligned bytes; counted toward the wave the op runs in)
# --------------------------------------------------------------------------- #


def _ws_zero(types, shapes, attrs, alignment):
    return 0


def _ws_matmul(types, shapes, attrs, alignment):
    # Model a packed-B panel workspace, as a classic GEMM pack stage needs.
    _m, k = shapes[0].dims
    _k2, n = shapes[1].dims
    return align_up(types[0].itemsize * k * n, alignment)


def _ws_reduce(types, shapes, attrs, alignment):
    # Partial-accumulator scratch sized to the full input.
    return align_up(types[0].itemsize * shapes[0].numel, alignment)


# --------------------------------------------------------------------------- #
# Kernels
# --------------------------------------------------------------------------- #


def _kernel_relu(inputs, attrs, workspace, out_buffers):
    np.maximum(inputs[0], 0.0, out=out_buffers[0])
    return [out_buffers[0]]


def _kernel_add(inputs, attrs, workspace, out_buffers):
    np.add(inputs[0], inputs[1], out=out_buffers[0])
    return [out_buffers[0]]


def _kernel_mul(inputs, attrs, workspace, out_buffers):
    np.multiply(inputs[0], inputs[1], out=out_buffers[0])
    return [out_buffers[0]]


def _kernel_matmul(inputs, attrs, workspace, out_buffers):
    np.matmul(inputs[0], inputs[1], out=out_buffers[0])
    return [out_buffers[0]]


def _kernel_reshape(inputs, attrs, workspace, out_buffers):
    target = _attr_tuple_int(attrs, "shape")
    # numel re-checked at run time for dynamically-shaped inputs
    if int(np.prod(target)) != inputs[0].size:
        raise InputValidationError(
            "reshape cannot change element count at run time",
            input_numel=int(inputs[0].size),
            output_numel=int(np.prod(target)),
        )
    # A view: the executor aliases this output to the input's slot, so the
    # returned ndarray shares storage with its source.
    return [inputs[0].reshape(target)]


def _kernel_transpose(inputs, attrs, workspace, out_buffers):
    x = inputs[0]
    if "perm" in attrs:
        perm = _attr_tuple_int(attrs, "perm")
        return [x.transpose(perm)]  # a view: shares storage
    return [x.T]


def _kernel_reduce_sum(inputs, attrs, workspace, out_buffers):
    axis = _attr_int(attrs, "axis")
    keep = bool(attrs.get("keepdims", False))
    np.sum(inputs[0], axis=axis, keepdims=keep, out=out_buffers[0])
    return [out_buffers[0]]


REGISTRY: dict[str, OpSpec] = {
    "relu": OpSpec(
        name="relu", ninputs=1, noutputs=1,
        infer=_infer_unary, kernel=_kernel_relu,
        workspace_fn=_ws_zero, alias=(None,),
    ),
    "add": OpSpec(
        name="add", ninputs=2, noutputs=1,
        infer=_infer_binary, kernel=_kernel_add,
        workspace_fn=_ws_zero, alias=(None,),
    ),
    "mul": OpSpec(
        name="mul", ninputs=2, noutputs=1,
        infer=_infer_binary, kernel=_kernel_mul,
        workspace_fn=_ws_zero, alias=(None,),
    ),
    "matmul": OpSpec(
        name="matmul", ninputs=2, noutputs=1,
        infer=_infer_matmul, kernel=_kernel_matmul,
        workspace_fn=_ws_matmul, alias=(None,),
        shape_sensitive=True,
    ),
    "reshape": OpSpec(
        name="reshape", ninputs=1, noutputs=1,
        infer=_infer_reshape, kernel=_kernel_reshape,
        workspace_fn=_ws_zero, alias=(0,),
        shape_sensitive=True,
    ),
    "transpose": OpSpec(
        name="transpose", ninputs=1, noutputs=1,
        infer=_infer_transpose, kernel=_kernel_transpose,
        workspace_fn=_ws_zero, alias=(0,),
    ),
    "reduce_sum": OpSpec(
        name="reduce_sum", ninputs=1, noutputs=1,
        infer=_infer_reduce_sum, kernel=_kernel_reduce_sum,
        workspace_fn=_ws_reduce, alias=(None,),
        shape_sensitive=True,
    ),
}


def get_op(name: str) -> OpSpec:
    if name not in REGISTRY:
        raise InputValidationError(
            "unknown op", op=name, supported=sorted(REGISTRY)
        )
    return REGISTRY[name]


def op_workspace_bytes(
    spec: OpSpec,
    types: list[TensorType],
    shapes: list[Shape],
    attrs: dict[str, Any],
    alignment: int = DEFAULT_ALIGNMENT,
) -> int:
    return spec.workspace_fn(types, shapes, attrs, alignment)
