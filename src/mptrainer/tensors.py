"""Tensor-type layer: dtype registry, casting, and finite checks.

The trainer keeps three physically separate parameter/state families:

1. master weights   -- fp32, the only authoritative copy, updated by the optimizer
2. forward weights  -- low precision (fp16 by default), derived from the master
                       copy at every micro-step and discarded afterwards
3. optimizer state  -- fp32 momentum buffers, keyed like the master weights

This module owns the dtype vocabulary shared by all three.
"""

from __future__ import annotations

from typing import Mapping

import numpy as np

MASTER_DTYPE = np.float32
GRAD_DTYPE = np.float32  # unscaled gradients are accumulated in fp32

LOW_PRECISION_DTYPES: dict[str, np.dtype] = {
    "float16": np.dtype(np.float16),
    "bfloat16": np.dtype(np.float32),  # numpy has no bf16; emulated in fp32
    "float32": np.dtype(np.float32),   # escape hatch: full-precision forward
}

ParamDict = dict[str, np.ndarray]


def resolve_low_dtype(name: str) -> np.dtype:
    """Map a config dtype name to a numpy dtype, rejecting unknown names."""
    try:
        return LOW_PRECISION_DTYPES[name]
    except KeyError:
        raise ValueError(
            f"unknown low-precision dtype {name!r}; "
            f"supported: {sorted(LOW_PRECISION_DTYPES)}"
        ) from None


def cast_params(params: Mapping[str, np.ndarray], dtype: np.dtype) -> ParamDict:
    """Return a NEW dict of params cast to ``dtype`` (master copy untouched)."""
    return {name: value.astype(dtype) for name, value in params.items()}


def zeros_like(params: Mapping[str, np.ndarray], dtype=GRAD_DTYPE) -> ParamDict:
    """Fresh zero buffers shaped like ``params`` (used for accumulators)."""
    return {name: np.zeros(value.shape, dtype=dtype) for name, value in params.items()}


def any_nonfinite(arrays: Mapping[str, np.ndarray]) -> bool:
    """True if any array contains inf or nan (gradient-overflow predicate)."""
    return any(not np.all(np.isfinite(a)) for a in arrays.values())


def add_in_place(accum: ParamDict, grads: Mapping[str, np.ndarray]) -> None:
    """accum += grads, buffer-local mutation only (no shared state escapes)."""
    for name, g in grads.items():
        accum[name] += g


def unscale_grads(grads: Mapping[str, np.ndarray], scale: float) -> ParamDict:
    """Divide scaled low-precision grads by the loss scale, upcasting to fp32."""
    return {name: g.astype(GRAD_DTYPE) / scale for name, g in grads.items()}


def max_abs(params: Mapping[str, np.ndarray]) -> float:
    """Largest absolute element across all arrays (diagnostics/logging)."""
    return float(max(np.max(np.abs(a)) for a in params.values()))
