"""Dynamic loss scaling state machine.

Semantics (PyTorch-style, made explicit):

* ``record_overflow`` -- the current window is dropped; the scale is backed
  off immediately (``scale *= backoff_factor``, clamped at ``min_scale``) and
  the good-step streak resets to zero.
* ``record_good_step`` -- one *committed* optimizer step with finite
  gradients; after ``growth_interval`` consecutive good steps the scale grows
  (``scale *= growth_factor``), capped at the largest finite value the low
  precision dtype can hold.

Skipped windows do not advance the good-step streak.  State is immutable in
style: each ``record_*`` returns a fresh :class:`LossScaler`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from .config import ScalerConfig


@dataclass(frozen=True)
class LossScaler:
    cfg: ScalerConfig
    scale: float
    growth_tracker: int  # consecutive committed good steps

    @classmethod
    def initial(cls, cfg: ScalerConfig, lowp_max: float) -> "LossScaler":
        if cfg.init_scale > lowp_max:
            raise ValueError(
                f"init_scale {cfg.init_scale} exceeds low-precision max {lowp_max}"
            )
        return cls(cfg=cfg, scale=float(cfg.init_scale), growth_tracker=0)

    @property
    def inv_scale(self) -> float:
        return 1.0 / self.scale

    def record_overflow(self) -> "LossScaler":
        backed_off = max(self.cfg.min_scale, self.scale * self.cfg.backoff_factor)
        return replace(self, scale=float(backed_off), growth_tracker=0)

    def record_good_step(self, lowp_max: float) -> "LossScaler":
        tracker = self.growth_tracker + 1
        scale = self.scale
        if tracker >= self.cfg.growth_interval:
            scale = min(lowp_max, self.scale * self.cfg.growth_factor)
            tracker = 0
        return replace(self, scale=float(scale), growth_tracker=tracker)

    def to_state(self) -> dict:
        return {"scale": self.scale, "growth_tracker": self.growth_tracker}

    @classmethod
    def from_state(cls, cfg: ScalerConfig, state: dict) -> "LossScaler":
        return cls(cfg=cfg, scale=float(state["scale"]), growth_tracker=int(state["growth_tracker"]))
