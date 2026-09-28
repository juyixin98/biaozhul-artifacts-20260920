"""Checkpoint layer: full training-state persistence.

A checkpoint contains EVERYTHING needed to resume bit-identically:
- fp32 master weights and fp32 optimizer (momentum) state  -> arrays.npz
- dynamic loss-scaler state (scale + growth counter)       -> state.json
- step counters (micro_step, optimizer_step)               -> state.json
- the validated trainer config                             -> state.json

The scaler state is first-class: losing it would silently change the
loss scale after resume and corrupt the comparison against an
uninterrupted run.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .config import TrainerConfig
from .runlog import runtime_versions
from .scaler import DynamicLossScaler
from .trainer import MixedPrecisionTrainer

ARRAYS_FILE = "arrays.npz"
STATE_FILE = "state.json"


def save_checkpoint(trainer: MixedPrecisionTrainer, directory: str | Path) -> Path:
    """Write the full training state; returns the checkpoint directory."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    arrays = {f"master::{k}": v for k, v in trainer.master.items()}
    arrays.update({f"opt::{k}": v for k, v in trainer.opt_state.items()})
    np.savez(directory / ARRAYS_FILE, **arrays)

    state = {
        "format": 1,
        "versions": runtime_versions(),
        "run_id": trainer.run_id,
        "micro_step": trainer.micro_step,
        "optimizer_step": trainer.optimizer_step,
        "window_position": trainer._window_count,
        "scaler": trainer.scaler.state_dict(),
        "optimizer": trainer.optimizer.state_dict(),
        "config": trainer.config.to_dict(),
    }
    (directory / STATE_FILE).write_text(json.dumps(state, indent=2) + "\n")
    trainer.logger.log("checkpoint_saved", directory=str(directory))
    return directory


def load_checkpoint(
    directory: str | Path,
    run_id: str | None = None,
    log_dir: str | Path | None = None,
) -> MixedPrecisionTrainer:
    """Rebuild a trainer whose state is identical to the saved one."""
    directory = Path(directory)
    state = json.loads((directory / STATE_FILE).read_text())
    if state.get("format") != 1:
        raise ValueError(f"unsupported checkpoint format: {state.get('format')}")

    config = TrainerConfig.from_dict(state["config"])
    trainer = MixedPrecisionTrainer(
        config=config, run_id=run_id or state["run_id"], log_dir=log_dir
    )

    arrays = np.load(directory / ARRAYS_FILE)
    trainer.master = {
        k[len("master::"):]: arrays[k].astype(np.float32)
        for k in arrays.files if k.startswith("master::")
    }
    trainer.opt_state = {
        k[len("opt::"):]: arrays[k].astype(np.float32)
        for k in arrays.files if k.startswith("opt::")
    }
    trainer.scaler = DynamicLossScaler.from_state_dict(state["scaler"])
    trainer.micro_step = int(state["micro_step"])
    trainer.optimizer_step = int(state["optimizer_step"])
    trainer._window_count = int(state["window_position"])
    # A mid-window checkpoint resumes with an empty accumulator: the
    # in-flight partial window is intentionally NOT persisted (a window is
    # atomic -- it either committed before the checkpoint or it restarts).
    trainer.logger.log("checkpoint_loaded", directory=str(directory))
    return trainer
