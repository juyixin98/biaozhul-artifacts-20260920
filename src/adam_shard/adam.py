"""Shard-local Adam state and the per-element update rule.

Adam is elementwise, so applying it to disjoint flat slices is numerically
identical to applying it to the unflattened vector.  Everything here is
functional: callers receive fresh arrays instead of mutated state.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class AdamConfig:
    lr: float = 0.01
    beta1: float = 0.9
    beta2: float = 0.999
    eps: float = 1e-8

    def __post_init__(self) -> None:
        if self.lr <= 0:
            raise ValueError("lr must be positive")
        if not 0.0 <= self.beta1 < 1.0 or not 0.0 <= self.beta2 < 1.0:
            raise ValueError("betas must lie in [0, 1)")
        if self.eps <= 0:
            raise ValueError("eps must be positive")

    @classmethod
    def from_config(cls, cfg: dict | None) -> "AdamConfig":
        cfg = cfg or {}
        return cls(
            lr=float(cfg.get("lr", 0.01)),
            beta1=float(cfg.get("beta1", 0.9)),
            beta2=float(cfg.get("beta2", 0.999)),
            eps=float(cfg.get("eps", 1e-8)),
        )

    def to_dict(self) -> dict:
        return {"lr": self.lr, "beta1": self.beta1, "beta2": self.beta2, "eps": self.eps}


@dataclass(frozen=True)
class AdamShardState:
    """Optimizer state owned by one rank for its flat slice."""

    m: np.ndarray
    v: np.ndarray
    step: int

    @classmethod
    def zeros(cls, size: int, dtype: np.dtype) -> "AdamShardState":
        dtype = np.dtype(dtype)
        return cls(m=np.zeros(size, dtype=dtype), v=np.zeros(size, dtype=dtype), step=0)


def adam_step_slice(
    param: np.ndarray,
    grad: np.ndarray,
    state: AdamShardState,
    cfg: AdamConfig,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """One Adam update on a flat slice.

    Returns ``(new_param, new_m, new_v, new_step)``; inputs are never mutated.
    """

    param = np.asarray(param)
    grad = np.asarray(grad, dtype=param.dtype)
    if param.shape != grad.shape or state.m.shape != param.shape or state.v.shape != param.shape:
        raise ValueError("param/grad/m/v must share one shape")
    next_step = state.step + 1
    b1, b2 = cfg.beta1, cfg.beta2
    new_m = b1 * state.m + (1.0 - b1) * grad
    new_v = b2 * state.v + (1.0 - b2) * grad * grad
    bc1 = 1.0 - b1**next_step
    bc2 = 1.0 - b2**next_step
    m_hat = new_m / bc1
    v_hat = new_v / bc2
    new_param = param - cfg.lr * m_hat / (np.sqrt(v_hat) + cfg.eps)
    return new_param, new_m, new_v, next_step
