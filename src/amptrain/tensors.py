"""Tensor-type layer.

The central invariant of mixed precision lives here -- three physically
separate tensor families are never aliased:

1. ``MasterWeights``  -- fp32 parameters, the only values the optimizer mutates.
2. ``LowPrecisionParams`` -- cast copies used for every forward/backward pass;
   they are *derived* from the master weights and never written back.
3. ``OptimizerState`` -- fp32 momentum buffers, keyed parallel to the master
   weights (independent storage, not views).

Gradients additionally travel in their own two-stage form
(``ScaledGradients`` -> ``UnscaledGradients``); see :mod:`amptrain.graph`.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import MASTER_DTYPE, ModelConfig, PrecisionConfig

# Weight names in fixed layer order; used everywhere parameters are enumerated.
PARAM_NAMES = ("W1", "W2")


_DTYPE_ALIASES = {"float16": np.float16, "float32": np.float32}


def lowp_numpy_dtype(precision: PrecisionConfig) -> np.dtype:
    """Resolve the configured low-precision dtype string to a NumPy dtype."""
    if precision.lowp_dtype in _DTYPE_ALIASES:
        return np.dtype(_DTYPE_ALIASES[precision.lowp_dtype])
    dtype = np.dtype(precision.lowp_dtype)  # raises for unsupported strings
    if dtype.kind != "f":
        raise ValueError(f"lowp dtype must be floating point, got {dtype}")
    return dtype


def init_shapes(model: ModelConfig) -> dict[str, tuple[int, int]]:
    """Shape table for the two weight matrices."""
    return {
        "W1": (model.in_dim, model.hidden_dim),
        "W2": (model.hidden_dim, model.out_dim),
    }


def _initial_weights(
    model: ModelConfig, rng: np.random.Generator
) -> dict[str, np.ndarray]:
    """LeCun-style scaled normal initialization in fp32."""
    weights: dict[str, np.ndarray] = {}
    for name, shape in init_shapes(model).items():
        fan_in = shape[0]
        scale = np.sqrt(1.0 / fan_in)
        weights[name] = (rng.standard_normal(shape) * scale).astype(MASTER_DTYPE)
    return weights


@dataclass(frozen=True)
class MasterWeights:
    """fp32 master weights.  ``updated`` returns a new instance (immutability)."""

    matrices: dict[str, np.ndarray]

    @classmethod
    def initialize(cls, model: ModelConfig, rng: np.random.Generator) -> "MasterWeights":
        matrices = _initial_weights(model, rng)
        return cls(matrices=matrices)

    def cast_to_lowp(self, dtype: np.dtype) -> "LowPrecisionParams":
        """Derive the throwaway low-precision forward/backward parameters."""
        cast = {name: np.array(value, dtype=dtype) for name, value in self.matrices.items()}
        return LowPrecisionParams(matrices=cast, source_dtype=dtype)

    def updated(self, deltas: dict[str, np.ndarray]) -> "MasterWeights":
        """Return new master weights = current + deltas; inputs are untouched."""
        new_matrices = {
            name: np.array(self.matrices[name] + deltas[name], dtype=MASTER_DTYPE)
            for name in PARAM_NAMES
        }
        return MasterWeights(matrices=new_matrices)

    def copy(self) -> "MasterWeights":
        return MasterWeights(
            matrices={name: self.matrices[name].copy() for name in PARAM_NAMES}
        )


@dataclass(frozen=True)
class LowPrecisionParams:
    """Low-precision cast of the master weights.  Read-only by contract;
    the graph must never write into these matrices."""

    matrices: dict[str, np.ndarray]
    source_dtype: np.dtype


@dataclass(frozen=True)
class OptimizerState:
    """fp32 momentum buffers -- independent arrays, not views of weights."""

    momentum: dict[str, np.ndarray]

    @classmethod
    def zeros(cls, model: ModelConfig) -> "OptimizerState":
        shapes = init_shapes(model)
        return cls(
            momentum={
                name: np.zeros(shapes[name], dtype=MASTER_DTYPE) for name in PARAM_NAMES
            }
        )

    def copy(self) -> "OptimizerState":
        return OptimizerState(
            momentum={name: self.momentum[name].copy() for name in PARAM_NAMES}
        )


@dataclass(frozen=True)
class GradientAccumulator:
    """fp32 accumulator for *unscaled* gradients across an accumulation window.

    A fresh accumulator is created at the start of each window; micro-batch
    contributions are added with :meth:`add`, which returns a new instance so
    partial state is never mutated in place.
    """

    sums: dict[str, np.ndarray]
    micro_batches_seen: int

    @classmethod
    def empty(cls, model: ModelConfig) -> "GradientAccumulator":
        shapes = init_shapes(model)
        return cls(
            sums={
                name: np.zeros(shapes[name], dtype=MASTER_DTYPE) for name in PARAM_NAMES
            },
            micro_batches_seen=0,
        )

    def add(self, gradients: dict[str, np.ndarray]) -> "GradientAccumulator":
        new_sums = {
            name: np.array(self.sums[name] + gradients[name], dtype=MASTER_DTYPE)
            for name in PARAM_NAMES
        }
        return GradientAccumulator(sums=new_sums, micro_batches_seen=self.micro_batches_seen + 1)

    def average(self, window_size: int) -> dict[str, np.ndarray]:
        return {
            name: np.array(self.sums[name] / window_size, dtype=MASTER_DTYPE)
            for name in PARAM_NAMES
        }
