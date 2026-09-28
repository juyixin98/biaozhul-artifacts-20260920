"""Training state: the mixed-precision trainer.

Step semantics (explicit, tested):

* A **window** consists of exactly ``accumulation.micro_batches`` micro-batches.
* Each micro-batch runs a *fresh* master->low-precision cast, low-precision
  forward/scaled-loss/backward with staged finite checks, then unscale in
  fp32 and accumulation into an fp32 buffer.
* If **any** micro-batch overflows, the whole window is dropped atomically:
  the partially accumulated gradients are discarded, master weights and
  momentum are untouched, the LR schedule does NOT advance, and the loss
  scale backs off immediately.
* Otherwise the averaged fp32 gradient drives one fp32 SGD-momentum step on
  the master weights; only committed steps advance counters and LR schedule.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from . import graph
from .config import MASTER_DTYPE, ModelConfig, RunConfig
from .data import SyntheticFixture
from .errors import AmpTrainError, ErrorCode, NonFiniteTensorError
from .events import EventLog
from .optimizer import sgd_step
from .scaler import LossScaler
from .scheduler import LRScheduler
from .tensors import (
    GradientAccumulator,
    MasterWeights,
    OptimizerState,
    PARAM_NAMES,
    lowp_numpy_dtype,
)

STATUS_COMMITTED = "committed"
STATUS_SKIPPED = "skipped"


@dataclass(frozen=True)
class Counters:
    window_index: int = 0  # attempted windows (committed + skipped)
    committed_steps: int = 0  # optimizer updates actually applied
    skipped_windows: int = 0  # windows dropped due to overflow


@dataclass(frozen=True)
class WindowOutcome:
    """Verdict for one accumulation window."""

    window_index: int
    status: str  # STATUS_COMMITTED | STATUS_SKIPPED
    scale_before: float
    scale_after: float
    lr: float
    committed_steps: int
    skipped_windows: int
    micro_batches: int
    overflow_stage: str | None = None
    overflow_micro_index: int | None = None
    nonfinite_tensors: tuple[str, ...] = ()
    grad_norm: float | None = None
    mean_loss_fp32: float | None = None
    # True exactly when ``mean_loss_fp32`` is a trustworthy finite number.
    # A skipped window reports False rather than pretending the loss is NaN.
    loss_recorded: bool = False
    weight_norm: float | None = None

    @property
    def skipped(self) -> bool:
        return self.status == STATUS_SKIPPED


def _validate_batch(
    x: np.ndarray, y: np.ndarray, model: ModelConfig
) -> tuple[np.ndarray, np.ndarray]:
    """System-boundary validation; raises INPUT_INVALID, never silent."""
    if not isinstance(x, np.ndarray) or not isinstance(y, np.ndarray):
        raise AmpTrainError(
            ErrorCode.INPUT_INVALID, "x and y must be numpy arrays",
            detail={"x_type": type(x).__name__, "y_type": type(y).__name__},
        )
    if x.ndim != 2 or y.ndim != 2:
        raise AmpTrainError(
            ErrorCode.INPUT_INVALID, "x and y must be 2-D arrays",
            detail={"x_ndim": x.ndim, "y_ndim": y.ndim},
        )
    if x.shape[0] != y.shape[0] or x.shape[0] == 0:
        raise AmpTrainError(
            ErrorCode.INPUT_INVALID, "x and y must share a positive row count",
            detail={"x_rows": int(x.shape[0]), "y_rows": int(y.shape[0])},
        )
    if x.shape[1] != model.in_dim:
        raise AmpTrainError(
            ErrorCode.INPUT_INVALID, "x feature count must equal model.in_dim",
            detail={"x_features": int(x.shape[1]), "in_dim": model.in_dim},
        )
    if y.shape[1] != model.out_dim:
        raise AmpTrainError(
            ErrorCode.INPUT_INVALID, "y target count must equal model.out_dim",
            detail={"y_targets": int(y.shape[1]), "out_dim": model.out_dim},
        )
    if not (np.isfinite(x).all() and np.isfinite(y).all()):
        raise AmpTrainError(
            ErrorCode.INPUT_INVALID,
            "input contains non-finite values; overflow must be induced via "
            "finite amplified inputs, not inf/NaN",
        )
    return x.astype(MASTER_DTYPE), y.astype(MASTER_DTYPE)


class MixedPrecisionTrainer:
    """Holds all training state and processes accumulation windows."""

    def __init__(
        self,
        config: RunConfig,
        *,
        run_id: str | None = None,
        fixture: SyntheticFixture | None = None,
        log_sink: Path | None = None,
        _resume: tuple | None = None,
    ):
        self.config = config
        self.run_id = run_id or uuid.uuid4().hex
        self.fixture = fixture
        self.log = EventLog(self.run_id, sink=log_sink)
        self.lowp_dtype = lowp_numpy_dtype(config.precision)
        self.lowp_max = float(np.finfo(self.lowp_dtype).max)
        self._fixture_cursor = 0

        if _resume is None:
            init_rng = np.random.default_rng(config.seed)
            self.master = MasterWeights.initialize(config.model, init_rng)
            self.opt_state = OptimizerState.zeros(config.model)
            self.scaler = LossScaler.initial(config.scaler, self.lowp_max)
            self.scheduler = LRScheduler(config.optimizer)
            self.counters = Counters()
            self._init_rng = init_rng
        else:
            (
                self.master,
                self.opt_state,
                self.scaler,
                self.scheduler,
                self.counters,
                rng_state,
                self._fixture_cursor,
            ) = _resume
            self._init_rng = np.random.default_rng()
            if rng_state is not None:
                self._init_rng.bit_generator.state = rng_state

    # ------------------------------------------------------------------ stats

    def stats(self) -> dict:
        return {
            "run_id": self.run_id,
            "window_index": self.counters.window_index,
            "committed_steps": self.counters.committed_steps,
            "skipped_windows": self.counters.skipped_windows,
            "scale": self.scaler.scale,
            "growth_tracker": self.scaler.growth_tracker,
            "lr": self.scheduler.learning_rate(),
            "lowp_dtype": self.config.precision.lowp_dtype,
            "micro_batches": self.config.accumulation.micro_batches,
        }

    # ------------------------------------------------------------- processing

    def process_window(self, batches: list[tuple[np.ndarray, np.ndarray]]) -> WindowOutcome:
        """Process one accumulation window atomically.

        ``batches`` must contain exactly ``micro_batches`` micro-batches.
        Overflow in any of them skips the entire window.
        """
        expected = self.config.accumulation.micro_batches
        if len(batches) != expected:
            raise AmpTrainError(
                ErrorCode.INPUT_INVALID,
                f"window needs exactly {expected} micro-batches, got {len(batches)}",
                detail={"expected": expected, "got": len(batches)},
            )

        scale_before = self.scaler.scale
        accumulator = GradientAccumulator.empty(self.config.model)
        overflow: graph.OverflowResult | None = None
        overflow_micro = -1
        finite_losses: list[float] = []
        all_losses_finite = True

        for micro_index, (x_raw, y_raw) in enumerate(batches):
            x, y = _validate_batch(x_raw, y_raw, self.config.model)
            params = self.master.cast_to_lowp(self.lowp_dtype)

            cache, ov = graph.forward(params, x, self.config.model.activation)
            if ov.overflowed:
                overflow, overflow_micro = ov, micro_index
                break

            loss_scaled, d_y, ov = graph.scaled_mse_loss(cache, y, self.scaler.scale)
            loss32 = graph.loss_fp32(self.master.matrices, x, y, self.config.model.activation)
            if not math.isfinite(loss32):
                all_losses_finite = False
            else:
                finite_losses.append(loss32)
            if ov.overflowed:
                overflow, overflow_micro = ov, micro_index
                break

            grads, ov = graph.backward(params, cache, d_y, self.config.model.activation)
            if ov.overflowed:
                overflow, overflow_micro = ov, micro_index
                break
            assert grads is not None  # guaranteed by the overflow check above

            unscaled = grads.unscale(self.scaler.scale)
            bad = graph.find_nonfinite(unscaled)
            if bad:
                # Unreachable if staged checks are correct; fail loudly rather
                # than silently feeding NaN into an optimizer step.
                raise NonFiniteTensorError("unscale", bad)
            accumulator = accumulator.add(unscaled)

        window_index = self.counters.window_index + 1

        if overflow is not None:
            return self._skip_window(
                window_index, scale_before, overflow, overflow_micro
            )
        if accumulator.micro_batches_seen != expected:
            # Bookkeeping invariant: no overflow means every micro-batch landed.
            raise NonFiniteTensorError(
                "accumulation",
                [f"expected {expected} micro-batches, got {accumulator.micro_batches_seen}"],
            )
        return self._commit_window(
            window_index, scale_before, accumulator, finite_losses, all_losses_finite
        )

    def _skip_window(
        self,
        window_index: int,
        scale_before: float,
        overflow: graph.OverflowResult,
        overflow_micro: int,
    ) -> WindowOutcome:
        # The partial accumulator goes out of scope here and is garbage
        # collected: nothing it held is committed to weights or momentum.
        self.scaler = self.scaler.record_overflow()
        self.counters = replace(
            self.counters,
            window_index=window_index,
            skipped_windows=self.counters.skipped_windows + 1,
        )
        outcome = WindowOutcome(
            window_index=window_index,
            status=STATUS_SKIPPED,
            overflow_stage=overflow.stage,
            overflow_micro_index=overflow_micro,
            nonfinite_tensors=overflow.nonfinite_tensors,
            scale_before=scale_before,
            scale_after=self.scaler.scale,
            lr=self.scheduler.learning_rate(),
            committed_steps=self.counters.committed_steps,
            skipped_windows=self.counters.skipped_windows,
            micro_batches=self.config.accumulation.micro_batches,
            loss_recorded=False,
        )
        self.log.append(
            "window_skipped",
            {
                "basis": "staged_finite_check",
                "overflow_stage": overflow.stage,
                "overflow_micro_index": overflow_micro,
                "nonfinite_tensors": list(overflow.nonfinite_tensors),
                "scale_before": scale_before,
                "scale_after": self.scaler.scale,
                "committed_steps": self.counters.committed_steps,
                "decision": "drop_window_no_partial_commit_backoff_scale",
            },
            window_index=window_index,
            micro_index=overflow_micro,
        )
        return outcome

    def _commit_window(
        self,
        window_index: int,
        scale_before: float,
        accumulator: GradientAccumulator,
        finite_losses: list[float],
        all_losses_finite: bool,
    ) -> WindowOutcome:
        gradients = accumulator.average(self.config.accumulation.micro_batches)
        lr = self.scheduler.learning_rate()
        new_master, new_state = sgd_step(
            self.master, self.opt_state, gradients, self.config.optimizer, lr
        )
        self.master = new_master
        self.opt_state = new_state
        self.scheduler = self.scheduler.advanced()
        self.scaler = self.scaler.record_good_step(self.lowp_max)
        self.counters = replace(
            self.counters,
            window_index=window_index,
            committed_steps=self.counters.committed_steps + 1,
        )
        # Stats are computed in float64 so they stay finite even when the
        # fp32/fp16 values are large; logging must never raise or overflow.
        grad_norm = float(
            np.sqrt(sum(float(np.sum(g.astype(np.float64) ** 2)) for g in gradients.values()))
        )
        weight_norm = float(
            np.sqrt(
                sum(
                    float(np.sum(w.astype(np.float64) ** 2))
                    for w in self.master.matrices.values()
                )
            )
        )
        mean_loss = float(np.mean(finite_losses)) if finite_losses else None
        outcome = WindowOutcome(
            window_index=window_index,
            status=STATUS_COMMITTED,
            scale_before=scale_before,
            scale_after=self.scaler.scale,
            lr=lr,
            committed_steps=self.counters.committed_steps,
            skipped_windows=self.counters.skipped_windows,
            micro_batches=self.config.accumulation.micro_batches,
            grad_norm=grad_norm,
            mean_loss_fp32=mean_loss,
            loss_recorded=all_losses_finite and mean_loss is not None,
            weight_norm=weight_norm,
        )
        self.log.append(
            "window_committed",
            {
                "basis": "all_stages_finite",
                "scale_before": scale_before,
                "scale_after": self.scaler.scale,
                "lr": lr,
                "committed_step": self.counters.committed_steps,
                "grad_norm_fp32": grad_norm,
                "weight_norm_fp32": weight_norm,
                "mean_loss_fp32": mean_loss,
            },
            window_index=window_index,
        )
        return outcome

    # -------------------------------------------------------- fixture driving

    def next_fixture_window(self) -> list[tuple[np.ndarray, np.ndarray]]:
        """Slice the next nominal window out of the attached fixture."""
        if self.fixture is None:
            raise AmpTrainError(
                ErrorCode.CONFIG_INVALID, "trainer has no attached synthetic fixture"
            )
        batches: list[tuple[np.ndarray, np.ndarray]] = []
        for _ in range(self.config.accumulation.micro_batches):
            batches.append(
                self.fixture.window(self._fixture_cursor, self.config.batch_size)
            )
            self._fixture_cursor += self.config.batch_size
        return batches

    def train_fixture_windows(self, n_windows: int) -> list[WindowOutcome]:
        outcomes = [self.process_window(self.next_fixture_window()) for _ in range(n_windows)]
        return outcomes
