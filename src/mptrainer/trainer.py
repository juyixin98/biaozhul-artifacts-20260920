"""Trainer layer: step semantics, gradient accumulation, overflow policy.

State separation (the core constraint of this implementation):
- ``master``        fp32 authoritative weights (only thing the optimizer updates)
- low-precision     fp16 forward/backward params, re-derived from ``master``
                    every micro-step, never persisted
- ``opt_state``     fp32 momentum buffers
- ``scaler``        dynamic loss-scale state machine

Step semantics:
- ``micro_step``     increments on EVERY train_step call (forward/backward attempt)
- ``optimizer_step`` increments ONLY on a committed update; the LR schedule
                     is a pure function of ``optimizer_step``, so skipped
                     windows never advance (or corrupt) LR progress
- overflow           the ENTIRE accumulation window is discarded -- a window
                     is atomic, there is no partial commit
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from . import graph, tensors
from .config import TrainerConfig
from .optimizer import MomentumSGD
from .runlog import RunLogger
from .scaler import DynamicLossScaler

DECISION_COMMITTED = "committed"
DECISION_ACCUMULATING = "accumulating"
DECISION_SKIPPED_OVERFLOW = "skipped_overflow"


@dataclass(frozen=True)
class StepRecord:
    """Immutable, JSON-serialisable account of one train_step call."""

    run_id: str
    micro_step: int
    optimizer_step: int
    decision: str                 # committed | accumulating | skipped_overflow
    overflow: bool
    scale_before: float
    scale_after: float
    lr: float
    loss: float | None            # unscaled loss, None when it overflowed
    window_position: int          # 1..accum_steps within the current window
    reason: str                   # human-readable decision basis

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MixedPrecisionTrainer:
    def __init__(
        self,
        config: TrainerConfig,
        run_id: str,
        log_dir: str | Path | None = None,
        logger: RunLogger | None = None,
    ) -> None:
        self.config = config.validate()
        self.run_id = run_id
        self.low_dtype = tensors.resolve_low_dtype(self.config.low_dtype)
        self.master = graph.init_params(self.config.layer_sizes, self.config.seed)
        self.optimizer = MomentumSGD(self.config.momentum)
        self.opt_state = self.optimizer.init_state(self.master)
        self.scaler = DynamicLossScaler(
            scale=self.config.init_scale,
            growth_factor=self.config.growth_factor,
            backoff_factor=self.config.backoff_factor,
            growth_interval=self.config.growth_interval,
        )
        self.micro_step = 0
        self.optimizer_step = 0
        self._accum = tensors.zeros_like(self.master)
        self._window_count = 0
        self.logger = logger or RunLogger(run_id, log_dir)
        self.logger.log(
            "run_init",
            config=self.config.to_dict(),
            param_max_abs=tensors.max_abs(self.master),
        )

    # -- public API ------------------------------------------------------

    def lr(self) -> float:
        """Learning rate for the NEXT committed step (schedule of optimizer_step)."""
        return self.config.base_lr / (1.0 + self.config.lr_decay * self.optimizer_step)

    def train_step(self, x: np.ndarray, y: np.ndarray) -> StepRecord:
        """One micro-step: forward/backward in low precision, maybe commit."""
        scale_before = self.scaler.scale
        forward_params = tensors.cast_params(self.master, self.low_dtype)
        cache = graph.forward(forward_params, x, y, scale_before, self.low_dtype)
        grads = graph.backward(forward_params, cache, y, scale_before)
        overflow = not np.isfinite(cache.scaled_loss) or tensors.any_nonfinite(grads)
        self.micro_step += 1

        if overflow:
            record = self._handle_overflow(scale_before)
        else:
            record = self._handle_clean(grads, cache, scale_before)
        self.logger.log("train_step", **record.to_dict())
        return record

    def state_summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "micro_step": self.micro_step,
            "optimizer_step": self.optimizer_step,
            "scale": self.scaler.scale,
            "good_steps": self.scaler.good_steps,
            "lr": self.lr(),
            "window_position": self._window_count,
            "master_max_abs": tensors.max_abs(self.master),
        }

    # -- internals -------------------------------------------------------

    def _handle_overflow(self, scale_before: float) -> StepRecord:
        """Reject the whole window: no weight/state mutation can have happened."""
        discarded = self._window_count
        self._accum = tensors.zeros_like(self.master)
        self._window_count = 0
        scale_after = self.scaler.update(overflow=True)
        return StepRecord(
            run_id=self.run_id,
            micro_step=self.micro_step,
            optimizer_step=self.optimizer_step,
            decision=DECISION_SKIPPED_OVERFLOW,
            overflow=True,
            scale_before=scale_before,
            scale_after=scale_after,
            lr=self.lr(),
            loss=None,
            window_position=0,
            reason=(
                f"non-finite scaled loss or gradient; discarded "
                f"{discarded} accumulated micro-step(s), scale "
                f"{scale_before} -> {scale_after}"
            ),
        )

    def _handle_clean(
        self,
        grads: tensors.ParamDict,
        cache: graph.ForwardCache,
        scale_before: float,
    ) -> StepRecord:
        tensors.add_in_place(self._accum, tensors.unscale_grads(grads, scale_before))
        self._window_count += 1
        unscaled_loss = cache.scaled_loss / scale_before

        if self._window_count < self.config.accum_steps:
            return StepRecord(
                run_id=self.run_id,
                micro_step=self.micro_step,
                optimizer_step=self.optimizer_step,
                decision=DECISION_ACCUMULATING,
                overflow=False,
                scale_before=scale_before,
                scale_after=scale_before,
                lr=self.lr(),
                loss=unscaled_loss,
                window_position=self._window_count,
                reason=(
                    f"clean micro-step {self._window_count}/"
                    f"{self.config.accum_steps}; window not yet full"
                ),
            )

        # Window full: commit the averaged gradient as one optimizer step.
        mean_grads = {
            name: g / np.float32(self.config.accum_steps)
            for name, g in self._accum.items()
        }
        lr = self.lr()
        self.master, self.opt_state = self.optimizer.step(
            self.master, self.opt_state, mean_grads, lr
        )
        self.optimizer_step += 1
        self._accum = tensors.zeros_like(self.master)
        self._window_count = 0
        scale_after = self.scaler.update(overflow=False)
        return StepRecord(
            run_id=self.run_id,
            micro_step=self.micro_step,
            optimizer_step=self.optimizer_step,
            decision=DECISION_COMMITTED,
            overflow=False,
            scale_before=scale_before,
            scale_after=scale_after,
            lr=lr,
            loss=unscaled_loss,
            window_position=self.config.accum_steps,
            reason=(
                f"window complete; committed optimizer step "
                f"{self.optimizer_step} at lr={lr:.6g}"
            ),
        )
