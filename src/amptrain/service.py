"""Run registry: owns live trainer instances for the service process."""

from __future__ import annotations

import threading

import numpy as np

from .config import RunConfig
from .data import make_fixture
from .errors import AmpTrainError, ErrorCode
from .trainer import MixedPrecisionTrainer


class RunRegistry:
    """Thread-safe in-memory registry (demo service; no external database)."""

    def __init__(self) -> None:
        self._runs: dict[str, MixedPrecisionTrainer] = {}
        self._fixture_specs: dict[str, tuple] = {}
        self._lock = threading.Lock()

    def create(self, config: RunConfig, *, n_samples: int, amplification: float) -> MixedPrecisionTrainer:
        rng = np.random.default_rng(config.data.seed)
        fixture = make_fixture(
            config.data, n_samples, amplification=amplification, rng=rng
        )
        trainer = MixedPrecisionTrainer(config, fixture=fixture)
        with self._lock:
            self._runs[trainer.run_id] = trainer
            self._fixture_specs[trainer.run_id] = (
                config, n_samples, amplification, config.data.seed
            )
        trainer.log.append(
            "run_created",
            {
                "basis": "request",
                "n_samples": n_samples,
                "amplification": amplification,
                "config_summary": {
                    "lowp_dtype": config.precision.lowp_dtype,
                    "init_scale": config.scaler.init_scale,
                    "micro_batches": config.accumulation.micro_batches,
                    "lr": config.optimizer.lr,
                    "schedule": config.optimizer.schedule,
                },
            },
            window_index=0,
        )
        return trainer

    def get(self, run_id: str) -> MixedPrecisionTrainer:
        with self._lock:
            trainer = self._runs.get(run_id)
        if trainer is None:
            raise AmpTrainError(
                ErrorCode.RUN_NOT_FOUND,
                f"run '{run_id}' does not exist in this process",
                detail={"run_id": run_id},
            )
        return trainer

    def register_loaded(self, trainer: MixedPrecisionTrainer) -> None:
        with self._lock:
            self._runs[trainer.run_id] = trainer

    def rebuild_fixture(self, run_id: str):
        """Re-attach the originally configured synthetic fixture after load."""
        with self._lock:
            spec = self._fixture_specs.get(run_id)
        if spec is None:
            return None
        config, n_samples, amplification, seed = spec
        return make_fixture(
            config.data, n_samples,
            amplification=amplification,
            rng=np.random.default_rng(seed),
        )
