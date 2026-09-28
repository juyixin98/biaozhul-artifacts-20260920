"""Shared pytest fixtures: isolated tmp storage, config and synthetic data."""

from __future__ import annotations

import numpy as np
import pytest

from adam_shard.adam import AdamConfig
from adam_shard.fixtures import AppConfig, make_dataset, write_sample_dataset
from adam_shard.graph import GraphSpec
from adam_shard.tensor_types import resolve_dtype


@pytest.fixture
def spec() -> GraphSpec:
    # 3->5->4->3 : 47 weights + 12 biases = 59 params.
    # 59 is uneven for BOTH world sizes of interest: 59 = 2*29+1 = 3*19+2.
    return GraphSpec(dims=(3, 5, 4, 3), name="mlp-tanh-test")


@pytest.fixture
def adam_cfg() -> AdamConfig:
    return AdamConfig(lr=0.01, beta1=0.9, beta2=0.999, eps=1e-8)


@pytest.fixture
def app_config(tmp_path, spec) -> AppConfig:
    cfg = AppConfig(
        storage_root=str(tmp_path / "ckpt"),
        seed=20260927,
        dtype=resolve_dtype("float64"),
        spec=spec,
        n_samples=12,
        adam=AdamConfig(lr=0.01, beta1=0.9, beta2=0.999, eps=1e-8),
        train_steps=3,
        api_host="127.0.0.1",
        api_port=0,
    )
    write_sample_dataset(cfg, str(tmp_path / "sample.npz"))
    return cfg


@pytest.fixture
def dataset(spec, app_config) -> dict[str, np.ndarray]:
    return make_dataset(app_config.n_samples, spec.dims[0], spec, app_config.seed)
