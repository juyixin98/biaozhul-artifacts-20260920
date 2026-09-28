"""Independent reference implementation of Adam.

This is deliberately a *separate code path* from :mod:`adam_shard.adam`:
it operates on name-keyed tensors with bias-correction factors computed
inline instead of on flat slices via a shard state object.  Tests treat it
as the oracle, so reference answers must not be produced by the sharding
core under test.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .adam import AdamConfig


@dataclass
class ReferenceAdam:
    """Single-process, tensor-dict Adam keyed by stable parameter names."""

    cfg: AdamConfig
    moments: dict[str, tuple[np.ndarray, np.ndarray]]
    t: int = 0

    @classmethod
    def initialize(cls, params: dict[str, np.ndarray], cfg: AdamConfig) -> "ReferenceAdam":
        moments = {n: (np.zeros_like(a), np.zeros_like(a)) for n, a in params.items()}
        return cls(cfg=cfg, moments=moments, t=0)

    def step(self, params: dict[str, np.ndarray], grads: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        if set(grads) != set(params) or set(grads) != set(self.moments):
            raise ValueError("params/grads/moments must share identical name sets")
        self.t += 1
        out: dict[str, np.ndarray] = {}
        c = self.cfg
        for name in sorted(params):
            g = grads[name]
            if g.shape != params[name].shape:
                raise ValueError(f"gradient shape mismatch on {name}")
            m_prev, v_prev = self.moments[name]
            # Moments expressed as EMA deviation form (algebraically identical to
            # the b*x+(1-b)*g form used by the sharded core).
            m_new = m_prev + (1.0 - c.beta1) * (g - m_prev)
            v_new = v_prev + (1.0 - c.beta2) * (g * g - v_prev)
            hat_m = m_new / (1.0 - c.beta1**self.t)
            hat_v = v_new / (1.0 - c.beta2**self.t)
            denom = np.sqrt(hat_v) + c.eps
            out[name] = params[name] - c.lr * hat_m / denom
            self.moments[name] = (m_new, v_new)
        return out
