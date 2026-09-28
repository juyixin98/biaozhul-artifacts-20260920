"""Optimizer layer: SGD with momentum over fp32 master weights.

The optimizer only ever sees fp32 master weights and fp32 (unscaled,
accumulated) gradients. It returns NEW weight/state dicts instead of
mutating in place, so a rejected update can never half-apply.
"""

from __future__ import annotations

from typing import Mapping

import numpy as np

from .tensors import ParamDict, zeros_like


class MomentumSGD:
    def __init__(self, momentum: float = 0.9) -> None:
        if not 0.0 <= momentum < 1.0:
            raise ValueError("momentum must be in [0, 1)")
        self.momentum = momentum

    def init_state(self, master: Mapping[str, np.ndarray]) -> ParamDict:
        """Fresh velocity buffers, one per master tensor (fp32)."""
        return zeros_like(master)

    def step(
        self,
        master: Mapping[str, np.ndarray],
        state: Mapping[str, np.ndarray],
        grads: Mapping[str, np.ndarray],
        lr: float,
    ) -> tuple[ParamDict, ParamDict]:
        """One SGD+momentum update; returns (new_master, new_state).

        v <- momentum * v + g
        w <- w - lr * v
        """
        new_state: ParamDict = {}
        new_master: ParamDict = {}
        for name, w in master.items():
            v = self.momentum * state[name] + grads[name].astype(np.float32)
            new_state[name] = v
            new_master[name] = (w - np.float32(lr) * v).astype(np.float32)
        return new_master, new_state

    def state_dict(self) -> dict:
        return {"momentum": self.momentum}
