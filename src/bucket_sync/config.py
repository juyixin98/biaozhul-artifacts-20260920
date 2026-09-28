"""Runtime configuration and synthetic data fixtures.

Configuration is plain immutable data, independent of the tests that
consume it.  Synthetic fixtures are generated from fixed seeds so a run is
reproducible; no external services or real data are involved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np

from bucket_sync.graph import ParameterGraph, ParameterNode
from bucket_sync.tensor_types import TensorSpec
from bucket_sync.training import BIAS_PARAM, WEIGHT_PARAM, build_linear_graph


@dataclass(frozen=True)
class RuntimeConfig:
    in_features: int = 3
    bucket_size: int = 2  # small on purpose: forces multi-bucket layouts
    lr: float = 0.05
    heartbeat_timeout_s: float = 1.0
    round_timeout_s: float = 8.0


def make_graph(in_features: int) -> ParameterGraph:
    return build_linear_graph(in_features)


def make_graph_with_frozen_bias(in_features: int) -> ParameterGraph:
    """Graph where the bias is non-trainable: it must stay as a placeholder."""
    return ParameterGraph(
        "linear-mse-frozen-bias",
        [
            ParameterNode(TensorSpec(WEIGHT_PARAM, (in_features, 1)), trainable=True),
            ParameterNode(TensorSpec(BIAS_PARAM, (1,)), trainable=False),
        ],
    )


def initial_params(in_features: int, seed: int = 7) -> Dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    return {
        WEIGHT_PARAM: rng.normal(size=(in_features, 1)),
        BIAS_PARAM: np.array([0.1], dtype=np.float64),
    }


def make_shards(
    in_features: int, sizes: Tuple[int, ...], seed: int = 42
) -> List[Tuple[np.ndarray, np.ndarray]]:
    """Deterministic synthetic regression shards of *unequal* sizes.

    Data comes from a known linear relation plus noise, so joint-batch
    training has a well-defined optimum.
    """
    rng = np.random.default_rng(seed)
    true_w = rng.normal(size=(in_features, 1))
    true_b = 0.4
    shards: List[Tuple[np.ndarray, np.ndarray]] = []
    for n in sizes:
        x = rng.normal(size=(n, in_features))
        noise = 0.01 * rng.normal(size=(n, 1))
        y = x @ true_w + true_b + noise
        shards.append((x, y))
    return shards
