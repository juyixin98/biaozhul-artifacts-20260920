"""Shared pytest fixtures: fixed synthetic data, graph, layout, model."""

from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np
import pytest

from bucket_sync.bucketing import BucketLayout
from bucket_sync.config import (
    RuntimeConfig,
    initial_params,
    make_graph,
    make_shards,
)
from bucket_sync.coordinator import Coordinator
from bucket_sync.diagnostics import Diagnostics
from bucket_sync.training import ModelState


@pytest.fixture
def cfg() -> RuntimeConfig:
    return RuntimeConfig()


@pytest.fixture
def graph(cfg: RuntimeConfig):
    return make_graph(cfg.in_features)


@pytest.fixture
def layout(graph, cfg: RuntimeConfig) -> BucketLayout:
    return BucketLayout(graph, cfg.bucket_size)


@pytest.fixture
def model(graph, cfg: RuntimeConfig) -> ModelState:
    return ModelState(graph, initial_params(cfg.in_features), step=0)


@pytest.fixture
def coordinator(graph, layout, model, cfg: RuntimeConfig) -> Coordinator:
    return Coordinator(
        graph,
        layout,
        model,
        lr=cfg.lr,
        diagnostics=Diagnostics(),
        heartbeat_timeout=cfg.heartbeat_timeout_s,
    )


@pytest.fixture
def unequal_shards(cfg: RuntimeConfig) -> List[Tuple[np.ndarray, np.ndarray]]:
    # Deliberately unequal: 5, 3, 2 samples across three workers.
    return make_shards(cfg.in_features, (5, 3, 2), seed=42)


@pytest.fixture
def params0(cfg: RuntimeConfig) -> Dict[str, np.ndarray]:
    return initial_params(cfg.in_features)
