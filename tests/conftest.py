"""Shared pytest helpers: tiny deterministic state for shard round-trips."""
from __future__ import annotations

import numpy as np
import pytest

from adam_shards.sharding import save_checkpoint


@pytest.fixture
def small_state():
    """9 elements across two params, deliberately uneven (straddles shards)."""
    rng = np.random.default_rng(42)
    params = {
        "alpha": rng.standard_normal((5,)),
        "beta": rng.standard_normal((2, 2)),
    }
    moments = {
        "alpha": (rng.standard_normal((5,)), rng.standard_normal((5,)), 3),
        "beta": (rng.standard_normal((2, 2)), rng.standard_normal((2, 2)), 3),
    }
    return params, moments


@pytest.fixture
def saved_checkpoint(tmp_path, small_state):
    params, moments = small_state
    ckpt = tmp_path / "ckpt2"
    commit = save_checkpoint(str(ckpt), params, moments, world_size=2)
    return ckpt, commit, params, moments


def random_state(rng, shapes, step):
    params = {n: rng.standard_normal(s) for n, s in shapes.items()}
    moments = {
        n: (rng.standard_normal(s), rng.standard_normal(s) ** 2, step)
        for n, s in shapes.items()
    }
    return params, moments
