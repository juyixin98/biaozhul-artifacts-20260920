"""Learning-rate schedule.

Only *committed* optimizer steps advance the schedule; skipped
(overflow) windows leave learning-rate progress untouched.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import OptimizerConfig


@dataclass(frozen=True)
class LRScheduler:
    cfg: OptimizerConfig
    committed_steps: int = 0

    def learning_rate(self) -> float:
        if self.cfg.schedule == "constant":
            return self.cfg.lr
        drops = self.committed_steps // self.cfg.step_size
        return self.cfg.lr * (self.cfg.gamma ** drops)

    def advanced(self) -> "LRScheduler":
        return LRScheduler(cfg=self.cfg, committed_steps=self.committed_steps + 1)

    def to_state(self) -> dict:
        return {"committed_steps": self.committed_steps}

    @classmethod
    def from_state(cls, cfg: OptimizerConfig, state: dict) -> "LRScheduler":
        return cls(cfg=cfg, committed_steps=int(state["committed_steps"]))
