"""Optimiser update rules (pure functions over the rows that take a step).

Layer 3a. The rules here operate ONLY on rows that have a non-zero aggregated
gradient ("active" rows). The decision about which rows are active is made by
the training layer, so these functions never have to reason about untouched or
zero-gradient rows: by contract every row handed here takes exactly one step.

Two rules are provided:

* ``sgd``:            w <- w - lr * g
* ``sgd_momentum``:   v <- mu * v + g ;  w <- w - lr * v

Coupled L2 weight decay (when non-zero) is folded into ``g`` before the
momentum buffer is touched. Inputs are not mutated; fresh arrays are returned.
"""

from __future__ import annotations

import numpy as np

from .config import OptimizerConfig, OptimizerName


def _apply_weight_decay(
    grad: np.ndarray, weight: np.ndarray, weight_decay: float
) -> np.ndarray:
    if weight_decay == 0.0:
        return grad
    return grad + weight_decay * weight


def step_rows(
    weight: np.ndarray,
    momentum_buf: np.ndarray,
    grad: np.ndarray,
    cfg: OptimizerConfig,
) -> tuple[np.ndarray, np.ndarray]:
    """Apply one optimiser step to a stack of active rows.

    Parameters
    ----------
    weight:
        Current weight rows, shape ``(k, dim)``.
    momentum_buf:
        Current momentum buffer rows for the same ``k`` rows.
    grad:
        Already-aggregated, already-clipped gradient rows, shape ``(k, dim)``.
        Every row is assumed non-zero (caller filters zero rows).
    cfg:
        Validated optimiser configuration.

    Returns
    -------
    ``(new_weight, new_momentum)`` fresh arrays; inputs stay untouched.
    """

    if weight.shape != grad.shape or momentum_buf.shape != grad.shape:
        raise ValueError(
            f"shape mismatch: weight {weight.shape}, momentum {momentum_buf.shape}, "
            f"grad {grad.shape}"
        )

    effective_grad = _apply_weight_decay(grad, weight, cfg.weight_decay)

    if cfg.name is OptimizerName.SGD:
        # No momentum: buffer is conceptually always zero and is returned
        # unchanged (as zeros) so state never acquires spurious history.
        new_weight = weight - cfg.lr * effective_grad
        return new_weight, np.zeros_like(momentum_buf)

    if cfg.name is OptimizerName.SGD_MOMENTUM:
        new_momentum = cfg.momentum * momentum_buf + effective_grad
        new_weight = weight - cfg.lr * new_momentum
        return new_weight, new_momentum

    raise ValueError(f"unsupported optimiser: {cfg.name!r}")
