"""Service layer: run lifecycle on top of the trainer and checkpoint store."""

from __future__ import annotations

import os
import threading
from typing import Any

from .checkpoint import committed, load_checkpoint, save_checkpoint
from .config import TrainConfig
from .errors import (
    CheckpointError,
    InvalidRequestError,
    RunExistsError,
    RunNotFoundError,
)
from .trainer import Trainer


class TrainingService:
    def __init__(self, base_dir: str) -> None:
        self.base_dir = os.path.abspath(base_dir)
        os.makedirs(self.base_dir, exist_ok=True)
        # One training call per run at a time within this process; concurrent
        # callers would otherwise race on the atomic checkpoint commit.
        self._train_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _train_lock(self, run_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._train_locks.setdefault(run_id, threading.Lock())
        return lock

    # -------------------------------------------------------------- #
    # Paths / lookup
    # -------------------------------------------------------------- #

    def _run_dir(self, run_id: str) -> str:
        if not run_id or not run_id.replace("-", "").replace("_", "").isalnum():
            raise InvalidRequestError(
                "run_id must contain only letters, digits, '-' and '_'"
            )
        return os.path.join(self.base_dir, run_id)

    def list_runs(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not os.path.isdir(self.base_dir):
            return out
        for name in sorted(os.listdir(self.base_dir)):
            run_dir = os.path.join(self.base_dir, name)
            if not os.path.isdir(run_dir):
                continue
            entry: dict[str, Any] = {"run_id": name, "committed": committed(run_dir)}
            if committed(run_dir):
                try:
                    state = load_checkpoint(run_dir)
                    entry["steps_committed"] = len(state["loss_history"])
                    entry["global_step"] = (
                        state["loss_history"][-1]["step"]
                        if state["loss_history"]
                        else 0
                    )
                except CheckpointError as exc:
                    entry["status"] = f"corrupt: {exc}"
            out.append(entry)
        return out

    def _require_run_dir(self, run_id: str) -> str:
        run_dir = self._run_dir(run_id)
        if not os.path.isdir(run_dir):
            raise RunNotFoundError(f"unknown run: {run_id}")
        return run_dir

    # -------------------------------------------------------------- #
    # Lifecycle
    # -------------------------------------------------------------- #

    def create_run(self, run_id: str, cfg: TrainConfig, overwrite: bool = False) -> dict[str, Any]:
        run_dir = self._run_dir(run_id)
        if os.path.exists(run_dir) and not overwrite:
            raise RunExistsError(f"run already exists: {run_id}")
        os.makedirs(run_dir, exist_ok=True)

        trainer = Trainer.fresh(cfg)
        # Commit an initial checkpoint at step 0: recovery is possible even
        # if the process dies before the first periodic commit.
        save_checkpoint(run_dir, trainer.build_state())
        return {"run_id": run_id, "run_dir": run_dir, "state": trainer.snapshot()}

    def train(self, run_id: str, stop_after: int | None = None) -> dict[str, Any]:
        """Resume from disk and run up to ``stop_after`` optimizer steps."""
        run_dir = self._require_run_dir(run_id)
        with self._train_lock(run_id):
            trainer = Trainer.resume(run_dir)
            # Graceful API stop: progress reached in this call is durably
            # committed on return (a real OS kill bypasses this path and is
            # bounded by checkpoint_every instead).
            result = trainer.train(
                run_dir=run_dir, stop_after=stop_after, commit_at_end=True
            )
        snap = trainer.snapshot()
        return {
            "run_id": run_id,
            "steps_run_this_call": result.steps_run,
            "global_step": result.global_step,
            "finished": result.finished,
            "eval_loss": result.final_loss,
            "snapshot": snap,
        }

    def status(self, run_id: str) -> dict[str, Any]:
        run_dir = self._require_run_dir(run_id)
        trainer = Trainer.resume(run_dir)
        return {
            "run_id": run_id,
            "config": trainer.cfg.to_dict(),
            "total_steps": trainer.cfg.total_steps,
            **trainer.snapshot(),
            "history_len": len(trainer.loss_history),
        }

    def inspect_checkpoint(self, run_id: str) -> dict[str, Any]:
        """Return the raw committed-state summary (no dataset rebuild)."""
        run_dir = self._require_run_dir(run_id)
        state = load_checkpoint(run_dir)
        return {
            "run_id": run_id,
            "format_version": state["format_version"],
            "has_model": "model" in state,
            "has_optimizer_state": all(k in state["optimizer"] for k in ("vW", "vb")),
            "has_rng_state": "rng_state" in state,
            "has_cursor": "cursor" in state,
            "cursor": state["cursor"],
            "data_fingerprint": state["data_fingerprint"],
            "loss_history_len": len(state["loss_history"]),
        }

    def dataset_preview(self, cfg: TrainConfig) -> dict[str, Any]:
        from .synthetic import make_dataset

        ds = make_dataset(cfg)
        return {
            "n_samples": ds.X.shape[0],
            "n_features": ds.X.shape[1],
            "fingerprint": ds.fingerprint,
        }
