"""Dynamic loss scaler: scale state machine.

Semantics (mirroring the classic GradScaler behaviour):
- on overflow:  scale <- scale * backoff_factor, growth counter resets
- on clean committed step: growth counter += 1; when it reaches
  ``growth_interval`` consecutive clean commits, scale <- scale * growth_factor

The scaler is pure training state: it never touches weights or gradients,
and its full state round-trips through ``state_dict`` for checkpoints.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DynamicLossScaler:
    scale: float = 1024.0
    growth_factor: float = 2.0
    backoff_factor: float = 0.5
    growth_interval: int = 2000
    good_steps: int = 0

    def __post_init__(self) -> None:
        if self.scale < 1.0:
            raise ValueError("initial scale must be >= 1")
        if not 0.0 < self.backoff_factor < 1.0:
            raise ValueError("backoff_factor must be in (0, 1)")
        if self.growth_factor <= 1.0:
            raise ValueError("growth_factor must be > 1")
        if self.growth_interval < 1:
            raise ValueError("growth_interval must be >= 1")

    def update(self, overflow: bool) -> float:
        """Advance the state machine; return the (possibly new) scale."""
        if overflow:
            self.scale = max(self.scale * self.backoff_factor, 1.0)
            self.good_steps = 0
            return self.scale
        self.good_steps += 1
        if self.good_steps >= self.growth_interval:
            self.scale *= self.growth_factor
            self.good_steps = 0
        return self.scale

    def state_dict(self) -> dict:
        return {
            "scale": self.scale,
            "growth_factor": self.growth_factor,
            "backoff_factor": self.backoff_factor,
            "growth_interval": self.growth_interval,
            "good_steps": self.good_steps,
        }

    @classmethod
    def from_state_dict(cls, state: dict) -> "DynamicLossScaler":
        return cls(
            scale=float(state["scale"]),
            growth_factor=float(state["growth_factor"]),
            backoff_factor=float(state["backoff_factor"]),
            growth_interval=int(state["growth_interval"]),
            good_steps=int(state["good_steps"]),
        )
