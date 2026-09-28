"""Compute graph: low-precision forward/backward for a 2-layer MLP.

All trainable matmuls run in the *low-precision* dtype using the cast
``LowPrecisionParams`` (never the fp32 master weights).  Every stage that can
overflow is checked explicitly with :func:`find_nonfinite`, and overflow is
reported as a classified :class:`OverflowResult` -- never raised, never turned
into a silent "success".

Network (no biases):

    Z1 = X @ W1
    A1 = activation(Z1)
    Y  = A1 @ W2
    L_scaled = mean((Y - T)^2) * scale        # MSE, computed in low precision
    dY = 2 * (Y - T) / N * scale              # low precision
    g2 = A1.T @ dY                            # low precision
    g1 = X.T @ (dY @ W2.T * activation')      # low precision
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

import numpy as np

from .tensors import LowPrecisionParams


@contextmanager
def _quiet_fp_warnings():
    """Overflow/NaN are expected in the checked low-precision path and are
    classified explicitly afterwards, so silence NumPy's scalar warnings."""
    with np.errstate(over="ignore", invalid="ignore"):
        yield

# Checked compute stages, in execution order.
STAGE_FORWARD = "forward_output"
STAGE_SCALED_LOSS = "scaled_loss"
STAGE_BACKWARD = "backward_grad"
COMPUTE_STAGES = (STAGE_FORWARD, STAGE_SCALED_LOSS, STAGE_BACKWARD)


def find_nonfinite(named: dict[str, np.ndarray]) -> list[str]:
    """Return names of arrays containing inf/-inf/NaN (empty == all finite)."""
    return [name for name, value in named.items() if not np.all(np.isfinite(value))]


def _relu(z: np.ndarray) -> np.ndarray:
    return np.maximum(z, 0)


def _activation(z: np.ndarray, kind: str) -> np.ndarray:
    if kind == "relu":
        return _relu(z)
    if kind == "tanh":
        return np.tanh(z)
    raise ValueError(f"unsupported activation: {kind}")


@dataclass(frozen=True)
class ForwardCache:
    """Intermediate low-precision activations consumed by the backward pass."""

    x: np.ndarray
    z1: np.ndarray
    a1: np.ndarray
    y: np.ndarray

    @property
    def source_dtype(self) -> np.dtype:
        return self.y.dtype


@dataclass(frozen=True)
class OverflowResult:
    """Overflow classification for one micro-batch."""

    overflowed: bool
    stage: str | None
    nonfinite_tensors: tuple[str, ...]


@dataclass(frozen=True)
class ScaledGradients:
    """Low-precision gradients still carrying the loss scale."""

    matrices: dict[str, np.ndarray]

    def unscale(self, scale: float) -> dict[str, np.ndarray]:
        """Divide the scale out in fp32.  Caller guarantees finiteness."""
        return {
            name: (value.astype(np.float32) / np.float32(scale))
            for name, value in self.matrices.items()
        }


def forward(
    params: LowPrecisionParams, x: np.ndarray, activation: str
) -> tuple[ForwardCache, OverflowResult]:
    """Low-precision forward pass with an explicit output-stage finite check."""
    with _quiet_fp_warnings():
        x_lp = x.astype(params.source_dtype)
        z1 = x_lp @ params.matrices["W1"]
        a1 = _activation(z1, activation)
        y = a1 @ params.matrices["W2"]

    bad = find_nonfinite({"z1": z1, "a1": a1, "y": y})
    if bad:
        return ForwardCache(x_lp, z1, a1, y), OverflowResult(True, STAGE_FORWARD, tuple(bad))
    return ForwardCache(x_lp, z1, a1, y), OverflowResult(False, None, ())


def scaled_mse_loss(
    cache: ForwardCache, target: np.ndarray, scale: float
) -> tuple[np.ndarray, np.ndarray, OverflowResult]:
    """Return ``(loss_scaled_lp, dY_scaled_lp, overflow)``.

    Both the scaled loss and its upstream gradient are computed in low
    precision so an excessive scale overflows here where it is detected.
    An fp32 loss is returned separately for logging (see trainer).
    """
    dt = cache.source_dtype
    t_lp = target.astype(dt)
    n = np.array(t_lp.shape[0], dtype=dt)
    diff = cache.y - t_lp
    with _quiet_fp_warnings():
        loss = np.sum(diff * diff) / n
        loss_scaled = (loss * np.array(scale, dtype=dt)).astype(dt)

    if not np.isfinite(loss_scaled):
        overflow = OverflowResult(True, STAGE_SCALED_LOSS, ("loss_scaled",))
        return loss_scaled, np.array(np.inf, dtype=dt), overflow

    d_y = (np.array(2.0, dtype=dt) * diff / n * np.array(scale, dtype=dt)).astype(dt)
    if not np.isfinite(d_y).all():
        overflow = OverflowResult(True, STAGE_SCALED_LOSS, ("dY",))
        return loss_scaled, d_y, overflow
    return loss_scaled, d_y, OverflowResult(False, None, ())


def loss_fp32(
    master_matrices: dict[str, np.ndarray],
    x: np.ndarray,
    target: np.ndarray,
    activation: str,
) -> float:
    """Unscaled MSE evaluated entirely in fp32, for logging/validation only.

    Deliberately independent of the low-precision forward pass so that
    reported losses are not corrupted by the precision being tested.
    """
    x32 = x.astype(np.float32)
    w1 = master_matrices["W1"].astype(np.float32)
    w2 = master_matrices["W2"].astype(np.float32)
    z1 = x32 @ w1
    a1 = _activation(z1, activation).astype(np.float32)
    y = a1 @ w2
    diff = y - target.astype(np.float32)
    with _quiet_fp_warnings():
        return float(np.mean(diff * diff))


def backward(
    params: LowPrecisionParams,
    cache: ForwardCache,
    d_y: np.ndarray,
    activation: str,
) -> tuple[ScaledGradients | None, OverflowResult]:
    """Low-precision backward pass; gradients are still loss-scaled."""
    w2 = params.matrices["W2"]
    if activation == "relu":
        mask = (cache.z1 > 0).astype(cache.source_dtype)
    else:
        mask = (1.0 - cache.a1 * cache.a1).astype(cache.source_dtype)

    with _quiet_fp_warnings():
        d_z1 = (d_y @ w2.T) * mask
        g2 = cache.a1.T @ d_y
        g1 = cache.x.T @ d_z1

    bad = find_nonfinite({"W1.grad": g1, "W2.grad": g2})
    if bad:
        return None, OverflowResult(True, STAGE_BACKWARD, tuple(bad))
    return ScaledGradients(matrices={"W1": g1, "W2": g2}), OverflowResult(False, None, ())
