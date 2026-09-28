"""Shared test builders (no pytest fixtures here)."""

from __future__ import annotations

import numpy as np

from amptrain.config import (
    AccumulationConfig,
    DataConfig,
    ModelConfig,
    OptimizerConfig,
    PrecisionConfig,
    RunConfig,
    ScalerConfig,
)


def make_config(**overrides) -> RunConfig:
    """Small deterministic network; defaults chosen so nothing overflows."""
    cfg = RunConfig(
        model=ModelConfig(in_dim=4, hidden_dim=6, out_dim=1, activation="relu"),
        precision=PrecisionConfig(lowp_dtype="float16"),
        scaler=ScalerConfig(init_scale=128.0, backoff_factor=0.5, growth_interval=2000),
        accumulation=AccumulationConfig(micro_batches=1),
        optimizer=OptimizerConfig(lr=0.01, momentum=0.9),
        data=DataConfig(n_features=4, noise_std=0.0, seed=77),
        seed=42,
        batch_size=8,
    )
    if not overrides:
        return cfg
    raw = {
        "model": cfg.model,
        "precision": cfg.precision,
        "scaler": cfg.scaler,
        "accumulation": cfg.accumulation,
        "optimizer": cfg.optimizer,
        "data": cfg.data,
        "seed": cfg.seed,
        "batch_size": cfg.batch_size,
    }
    raw.update(overrides)
    return RunConfig(**raw)


def normal_batches(config: RunConfig, n: int = 1, seed: int = 5):
    """Finite, unscaled synthetic micro-batches."""
    rng = np.random.default_rng(seed)
    w = rng.standard_normal((config.model.in_dim, 1)).astype(np.float32)
    batches = []
    for _ in range(n):
        x = rng.standard_normal((config.batch_size, config.model.in_dim)).astype(np.float32)
        y = (x @ w).astype(np.float32)
        batches.append((x, y))
    return batches


def amplified_batches(config: RunConfig, amplification: float, n: int = 1, seed: int = 5):
    """Finite but scaled-up batches that drive the lp path to overflow."""
    base = normal_batches(config, n=n, seed=seed)
    a = np.float32(amplification)
    return [(x * a, y * a) for x, y in base]


def snapshot_weights(trainer) -> dict[str, np.ndarray]:
    return {name: trainer.master.matrices[name].copy() for name in ("W1", "W2")}


def snapshot_momentum(trainer) -> dict[str, np.ndarray]:
    return {name: trainer.opt_state.momentum[name].copy() for name in ("W1", "W2")}


def weight_dicts_equal(a: dict[str, np.ndarray], b: dict[str, np.ndarray]) -> bool:
    return all(np.array_equal(a[name], b[name]) for name in ("W1", "W2"))
