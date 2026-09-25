"""Training core: mini-batch loop that can crash and resume at any step.

The trainer owns every piece of state needed for deterministic recovery:

* model parameters (``W``, ``b``)
* optimizer velocity (``vW``, ``vb``)
* the training RNG's full BitGenerator state
* the data cursor (epoch / position / permutation / global step)
* per-step loss history

Checkpoints are committed every ``cfg.checkpoint_every`` optimizer steps.
Training can be limited to ``stop_after`` steps in one call to simulate a
crash at an arbitrary batch; a real OS process kill relies on the same
on-disk commit (see scripts/run_demo.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from .checkpoint import load_checkpoint, save_checkpoint
from .config import FORMAT_VERSION, TrainConfig
from .cursor import DataCursor
from .errors import DatasetMismatchError
from .model import LinearModel
from .optimizer import MomentumSgd
from .synthetic import Dataset, make_dataset


@dataclass(frozen=True)
class TrainResult:
    steps_run: int
    global_step: int
    finished: bool
    final_loss: float


class Trainer:
    def __init__(
        self,
        cfg: TrainConfig,
        dataset: Dataset,
        model: LinearModel,
        optimizer: MomentumSgd,
        rng: np.random.Generator,
        cursor: DataCursor,
        loss_history: list[dict[str, Any]],
    ) -> None:
        self.cfg = cfg
        self.dataset = dataset
        self.model = model
        self.optimizer = optimizer
        self.rng = rng
        self.cursor = cursor
        # [{step: int, loss: float}, ...] one entry per completed step.
        self.loss_history = loss_history

    # ------------------------------------------------------------------ #
    # Construction / recovery
    # ------------------------------------------------------------------ #

    @classmethod
    def fresh(cls, cfg: TrainConfig, dataset: Dataset | None = None) -> "Trainer":
        dataset = dataset or make_dataset(cfg)
        dataset.verify()
        model = LinearModel(cfg.n_features, cfg.train_seed)
        optimizer = MomentumSgd(cfg.n_features, cfg.lr, cfg.momentum)
        # Independent stream from the init RNG (both seeded deterministically).
        rng = np.random.default_rng(cfg.train_seed)
        cursor = DataCursor.fresh(cfg.n_samples, rng)
        return cls(cfg, dataset, model, optimizer, rng, cursor, [])

    @classmethod
    def resume(cls, run_dir: str, cfg: TrainConfig | None = None) -> "Trainer":
        """Rebuild a trainer from the committed checkpoint in ``run_dir``.

        The dataset is regenerated from the checkpoint config and its
        fingerprint must match, otherwise resume is refused.
        """
        state = load_checkpoint(run_dir)

        saved_cfg = TrainConfig.from_dict(state["config"])
        if cfg is not None and cfg != saved_cfg:
            raise DatasetMismatchError(
                "resume config does not match the checkpoint config"
            )
        cfg = saved_cfg

        dataset = make_dataset(cfg)
        if dataset.fingerprint != state["data_fingerprint"]:
            raise DatasetMismatchError(
                "dataset fingerprint differs from checkpoint: refusing to resume"
            )

        model = LinearModel.from_params(state["model"]["W"], state["model"]["b"])
        opt_s = state["optimizer"]
        optimizer = MomentumSgd.from_state(
            opt_s["vW"], opt_s["vb"], opt_s["lr"], opt_s["momentum"]
        )
        rng = np.random.default_rng(0)
        rng.bit_generator.state = state["rng_state"]
        cursor = DataCursor.from_state(state["cursor"])
        history = [dict(item) for item in state["loss_history"]]
        return cls(cfg, dataset, model, optimizer, rng, cursor, history)

    def build_state(self) -> dict[str, Any]:
        return {
            "format_version": FORMAT_VERSION,
            "config": self.cfg.to_dict(),
            "model": {"W": self.model.W.copy(), "b": np.float32(self.model.b)},
            "optimizer": {
                "vW": self.optimizer.vW.copy(),
                "vb": np.float32(self.optimizer.vb),
                "lr": self.optimizer.lr,
                "momentum": self.optimizer.momentum,
            },
            "rng_state": self.rng.bit_generator.state,
            "cursor": self.cursor.to_state(),
            "data_fingerprint": self.dataset.fingerprint,
            "loss_history": [dict(item) for item in self.loss_history],
        }

    # ------------------------------------------------------------------ #
    # Training
    # ------------------------------------------------------------------ #

    def train(
        self,
        run_dir: str | None = None,
        stop_after: int | None = None,
        on_commit: Callable[[int], None] | None = None,
        on_step: Callable[[int, bool], None] | None = None,
        commit_at_end: bool = False,
    ) -> TrainResult:
        """Run optimizer steps until completion or ``stop_after`` this call.

        When ``run_dir`` is given, a checkpoint is committed after every
        ``checkpoint_every`` steps and at natural completion. ``on_step``
        fires after each step's checkpoint handling, so at a commit boundary
        the durable checkpoint precedes the callback.

        ``commit_at_end`` distinguishes two stop semantics:

        * ``False`` (default): a stop means a *crash*. Nothing beyond the
          periodic commits is persisted, so a resume replays the trailing
          uncommitted batches deterministically.
        * ``True``: a stop is a *graceful pause* (e.g. an API call with
          ``stop_after``); progress reached in this call is committed before
          returning, even mid-interval.
        """
        if stop_after is not None and stop_after < 0:
            raise ValueError("stop_after must be non-negative")

        steps_this_call = 0
        while self.cursor.global_step < self.cfg.total_steps:
            if stop_after is not None and steps_this_call == stop_after:
                break

            idx = self.cursor.next_indices(self.cfg.batch_size, self.rng)
            Xb = self.dataset.X[idx]
            yb = self.dataset.y[idx]

            loss, dW, db = self.model.loss_and_grad(Xb, yb, self.cfg.l2)
            self.model.W, self.model.b = self.optimizer.step(
                self.model.W, self.model.b, dW, db
            )

            self.loss_history.append(
                {"step": int(self.cursor.global_step), "loss": float(loss)}
            )
            steps_this_call += 1

            committed_this_step = False
            if run_dir is not None and (
                self.cursor.global_step % self.cfg.checkpoint_every == 0
                or self.cursor.global_step == self.cfg.total_steps
            ):
                save_checkpoint(run_dir, self.build_state())
                committed_this_step = True
                if on_commit is not None:
                    on_commit(int(self.cursor.global_step))

            if on_step is not None:
                # Callback may hard-kill the process (SIGKILL demo); at a
                # commit boundary the checkpoint is already durable above.
                on_step(int(self.cursor.global_step), committed_this_step)

        # Graceful pause with uncommitted progress: persist it so the next
        # API call resumes exactly here rather than replaying.
        if (
            commit_at_end
            and run_dir is not None
            and steps_this_call > 0
            and self.cursor.global_step % self.cfg.checkpoint_every != 0
            and self.cursor.global_step < self.cfg.total_steps
        ):
            save_checkpoint(run_dir, self.build_state())

        return TrainResult(
            steps_run=steps_this_call,
            global_step=int(self.cursor.global_step),
            finished=self.cursor.global_step >= self.cfg.total_steps,
            final_loss=self.evaluate(),
        )

    def evaluate(self) -> float:
        """Mean-squared error over the full dataset with the current params."""
        pred = self.model.predict(self.dataset.X)
        residual = pred - self.dataset.y
        return float(np.mean(residual * residual))

    def snapshot(self) -> dict[str, Any]:
        return {
            "global_step": int(self.cursor.global_step),
            "epoch": int(self.cursor.epoch),
            "pos": int(self.cursor.pos),
            "W": self.model.W.copy(),
            "b": float(self.model.b),
            "vW": self.optimizer.vW.copy(),
            "vb": float(self.optimizer.vb),
            "loss": self.evaluate(),
            "last_step_loss": (
                self.loss_history[-1]["loss"] if self.loss_history else None
            ),
        }
