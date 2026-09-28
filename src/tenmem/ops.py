"""Kernel registry: shape inference and numerical execution for every op.

Kernels are deliberately small, pure NumPy functions.  They serve two masters:

* the *planner* calls :data:`shape_fn` only with concrete runtime input shapes
  (the symbolic plan itself never needs this — buffers are sized from declared
  upper bounds);
* the *executor* calls :data:`fn` with concrete ``numpy.ndarray`` inputs and
  pre-allocated, correctly typed output/workspace views.

A kernel may declare ``workspace`` scratch bytes per call.  Workspace is live
for exactly the node's step and is accounted in peak memory; after the step it
is dead and its buffer may be reused like any other.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

# A kernel receives (inputs list, outputs list, workspace list-or-None, config).
KernelFn = Callable[[list[np.ndarray], list[np.ndarray], list[np.ndarray] | None, dict], None]
# Shape inference: (input shapes, config, declared output specs) -> output shapes.
ShapeFn = Callable[[list[tuple[int, ...]], dict, list], list[tuple[int, ...]]]


@dataclass(frozen=True)
class OpDef:
    name: str
    n_inputs: int
    n_outputs: int
    shape_fn: ShapeFn
    fn: KernelFn
    workspace_fn: Callable[[list[tuple[int, ...]], dict, int], int] | None = None
    # Input indices whose buffer may be handed off directly to this node's
    # output when this node is the input's LAST consumer: the kernel reads the
    # input before overwriting (or is otherwise alias-safe). matmul is excluded
    # — output geometry differs and GEMM is not in-place safe here.
    inplace_safe: tuple[int, ...] = (0,)


def _same_shape(input_shapes, config, outputs):
    return [input_shapes[0]]


def _matmul_shape(input_shapes, config, outputs):
    (m, k1), (k2, n) = input_shapes
    return [(m, n)]


def _matmul_workspace(input_shapes, config, itemsize):
    # Scratch tile sized to the output tile — demonstrates workspace pressure.
    m, n = input_shapes[0][0], input_shapes[1][1]
    return m * n * itemsize


def _dynamic_tile_shape(input_shapes, config, outputs):
    # input 0 is the data vector, input 1 is an int64 scalar = actual extent.
    # The concrete extent is passed positionally via
    # config["runtime_scalar_values"] by the executor. Shape functions are pure:
    # bound enforcement is the executor's job (replanning_required).
    k = int(config["runtime_scalar_values"][1])
    return [(k,)]


def _k_add(inputs, outputs, workspace, config):
    outputs[0][...] = np.add(inputs[0], inputs[1])


def _k_mul(inputs, outputs, workspace, config):
    outputs[0][...] = np.multiply(inputs[0], inputs[1])


def _k_relu(inputs, outputs, workspace, config):
    np.maximum(inputs[0], 0.0, out=outputs[0])


def _k_identity(inputs, outputs, workspace, config):
    np.copyto(outputs[0], inputs[0])


def _k_matmul(inputs, outputs, workspace, config):
    # Use the declared workspace explicitly so its liveness is real, not nominal:
    # accumulate there, then copy into the planned output view. The buffer
    # capacity is alignment-rounded, so take only the output-sized prefix.
    n = outputs[0].size
    scratch = workspace[0][:n].reshape(outputs[0].shape)
    np.matmul(inputs[0], inputs[1], out=scratch)
    np.copyto(outputs[0], scratch)


def _k_softmax(inputs, outputs, workspace, config):
    # workspace holds a shifted copy to stabilise the exponential.
    n = inputs[0].size
    tmp = workspace[0][:n].reshape(inputs[0].shape)
    np.subtract(inputs[0], np.max(inputs[0], axis=-1, keepdims=True), out=tmp)
    np.exp(tmp, out=outputs[0])
    outputs[0] /= np.sum(outputs[0], axis=-1, keepdims=True)


def _softmax_workspace(input_shapes, config, itemsize):
    return int(np.prod(input_shapes[0])) * itemsize


def _k_tile(inputs, outputs, workspace, config):
    np.copyto(outputs[0], inputs[0][: outputs[0].shape[0]])


REGISTRY: dict[str, OpDef] = {
    "add": OpDef("add", 2, 1, _same_shape, _k_add, inplace_safe=(0, 1)),
    "mul": OpDef("mul", 2, 1, _same_shape, _k_mul, inplace_safe=(0, 1)),
    "relu": OpDef("relu", 1, 1, _same_shape, _k_relu, inplace_safe=(0,)),
    "identity": OpDef("identity", 1, 1, _same_shape, _k_identity, inplace_safe=(0,)),
    "matmul": OpDef(
        "matmul", 2, 1, _matmul_shape, _k_matmul,
        workspace_fn=_matmul_workspace, inplace_safe=(),
    ),
    "softmax": OpDef(
        "softmax", 1, 1, _same_shape, _k_softmax,
        workspace_fn=_softmax_workspace, inplace_safe=(0,),
    ),
    "dynamic_tile": OpDef(
        "dynamic_tile", 2, 1, _dynamic_tile_shape, _k_tile, inplace_safe=(0,)
    ),
}


def get_op(name: str) -> OpDef:
    if name not in REGISTRY:
        raise KeyError(name)
    return REGISTRY[name]
