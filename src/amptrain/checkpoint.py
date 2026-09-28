"""Checkpoint persistence.

A checkpoint is a directory containing:

* ``meta.json``    -- format version, package version, run_id, config,
                      counters, scaler state (incl. scale + growth tracker),
                      LR-scheduler progress;
* ``weights.npz``  -- fp32 master weights and fp32 momentum buffers;
* ``rng.pkl``      -- data RNG state and fixture cursor (training resume).

Loading validates presence, shapes and dtypes; any mismatch raises
``CHECKPOINT_CORRUPT`` rather than silently producing a broken trainer.
The loss-scaler state is mandatory: without it resume would restart at the
initial scale and defeat dynamic scaling.
"""

from __future__ import annotations

import json
import pickle
from dataclasses import asdict
from pathlib import Path

import numpy as np

from .config import (
    AccumulationConfig,
    DataConfig,
    ModelConfig,
    OptimizerConfig,
    PrecisionConfig,
    RunConfig,
    ScalerConfig,
)
from .errors import AmpTrainError, ErrorCode
from .scaler import LossScaler
from .scheduler import LRScheduler
from .tensors import PARAM_NAMES, MasterWeights, OptimizerState
from .trainer import Counters, MixedPrecisionTrainer
from .version import VERSION

CHECKPOINT_FORMAT = "amptrain-ckpt-1"
META_NAME = "meta.json"
WEIGHTS_NAME = "weights.npz"
RNG_NAME = "rng.pkl"


def _config_from_dict(raw: dict) -> RunConfig:
    return RunConfig(
        model=ModelConfig(**raw["model"]),
        precision=PrecisionConfig(**raw["precision"]),
        scaler=ScalerConfig(**raw["scaler"]),
        accumulation=AccumulationConfig(**raw["accumulation"]),
        optimizer=OptimizerConfig(**raw["optimizer"]),
        data=DataConfig(**raw["data"]),
        seed=int(raw["seed"]),
        batch_size=int(raw["batch_size"]),
    )


def save_checkpoint(trainer: MixedPrecisionTrainer, directory: str | Path) -> Path:
    """Write a full checkpoint (incl. scaler state) to ``directory``."""
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)

    arrays: dict[str, np.ndarray] = {}
    for name in PARAM_NAMES:
        arrays[f"master_{name}"] = trainer.master.matrices[name]
        arrays[f"momentum_{name}"] = trainer.opt_state.momentum[name]
    np.savez(path / WEIGHTS_NAME, **arrays)

    meta = {
        "format": CHECKPOINT_FORMAT,
        "amptrain_version": VERSION,
        "run_id": trainer.run_id,
        "config": {
            "model": asdict(trainer.config.model),
            "precision": asdict(trainer.config.precision),
            "scaler": asdict(trainer.config.scaler),
            "accumulation": asdict(trainer.config.accumulation),
            "optimizer": asdict(trainer.config.optimizer),
            "data": asdict(trainer.config.data),
            "seed": trainer.config.seed,
            "batch_size": trainer.config.batch_size,
        },
        "counters": {
            "window_index": trainer.counters.window_index,
            "committed_steps": trainer.counters.committed_steps,
            "skipped_windows": trainer.counters.skipped_windows,
        },
        "scaler": trainer.scaler.to_state(),
        "scheduler": trainer.scheduler.to_state(),
        "fixture_cursor": trainer._fixture_cursor,
    }
    (path / META_NAME).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    with (path / RNG_NAME).open("wb") as handle:
        pickle.dump(trainer._init_rng.bit_generator.state, handle)

    trainer.log.append(
        "checkpoint_saved",
        {"basis": "full_state", "path": str(path), "scaler": trainer.scaler.to_state()},
        window_index=trainer.counters.window_index,
    )
    return path


def _require_meta(path: Path) -> dict:
    meta_path = path / META_NAME
    if not path.is_dir() or not meta_path.exists():
        raise AmpTrainError(
            ErrorCode.CHECKPOINT_NOT_FOUND,
            f"checkpoint not found at {path}",
            detail={"path": str(path)},
        )
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise AmpTrainError(
            ErrorCode.CHECKPOINT_CORRUPT, f"unreadable meta.json: {exc}",
            detail={"path": str(path)},
        ) from exc


def load_checkpoint(
    directory: str | Path, *, fixture=None
) -> MixedPrecisionTrainer:
    """Rebuild a trainer from a checkpoint and verify its integrity."""
    path = Path(directory)
    meta = _require_meta(path)

    try:
        if meta.get("format") != CHECKPOINT_FORMAT:
            raise AmpTrainError(
                ErrorCode.CHECKPOINT_CORRUPT,
                f"unexpected checkpoint format {meta.get('format')!r}",
                detail={"expected": CHECKPOINT_FORMAT},
            )
        config = _config_from_dict(meta["config"])

        weights_path = path / WEIGHTS_NAME
        rng_path = path / RNG_NAME
        if not weights_path.exists() or not rng_path.exists():
            raise AmpTrainError(
                ErrorCode.CHECKPOINT_CORRUPT,
                "checkpoint is missing weights.npz or rng.pkl",
            )

        with np.load(weights_path) as blob:
            master_m: dict[str, np.ndarray] = {}
            momentum_m: dict[str, np.ndarray] = {}
            for name in PARAM_NAMES:
                wkey, mkey = f"master_{name}", f"momentum_{name}"
                if wkey not in blob or mkey not in blob:
                    raise AmpTrainError(
                        ErrorCode.CHECKPOINT_CORRUPT,
                        f"missing array {wkey}/{mkey} in checkpoint",
                    )
                master_m[name] = np.array(blob[wkey], dtype=np.float32)
                momentum_m[name] = np.array(blob[mkey], dtype=np.float32)

        # Shape integrity against the declared config.
        from .tensors import init_shapes

        for name, expected in init_shapes(config.model).items():
            if master_m[name].shape != expected or momentum_m[name].shape != expected:
                raise AmpTrainError(
                    ErrorCode.CHECKPOINT_CORRUPT,
                    f"shape mismatch for {name}: "
                    f"{master_m[name].shape}/{momentum_m[name].shape} vs {expected}",
                )

        scaler_state = meta["scaler"]
        if not (np.isfinite(scaler_state["scale"]) and scaler_state["scale"] > 0):
            raise AmpTrainError(
                ErrorCode.CHECKPOINT_CORRUPT,
                "saved loss scale must be finite and positive",
                detail={"scale": scaler_state["scale"]},
            )

        with rng_path.open("rb") as handle:
            rng_state = pickle.load(handle)
    except AmpTrainError:
        raise
    except (KeyError, TypeError, pickle.UnpicklingError, OSError) as exc:
        raise AmpTrainError(
            ErrorCode.CHECKPOINT_CORRUPT, f"checkpoint payload unreadable: {exc}"
        ) from exc

    master = MasterWeights(matrices=master_m)
    opt_state = OptimizerState(momentum=momentum_m)
    scaler = LossScaler.from_state(config.scaler, meta["scaler"])
    scheduler = LRScheduler.from_state(config.optimizer, meta["scheduler"])
    counters = Counters(**meta["counters"])

    trainer = MixedPrecisionTrainer(
        config,
        run_id=meta["run_id"],
        fixture=fixture,
        _resume=(
            master,
            opt_state,
            scaler,
            scheduler,
            counters,
            rng_state,
            int(meta["fixture_cursor"]),
        ),
    )
    trainer.log.append(
        "checkpoint_loaded",
        {
            "basis": "validated_full_state",
            "path": str(path),
            "scaler": scaler.to_state(),
            "committed_steps": counters.committed_steps,
        },
        window_index=counters.window_index,
    )
    return trainer
