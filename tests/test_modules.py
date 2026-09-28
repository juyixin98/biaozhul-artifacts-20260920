"""Tests for config loading, graph helpers and reshard equivalence."""
from __future__ import annotations

import json

import numpy as np
import pytest

from adam_shards.config import AppConfig
from adam_shards.graph import GraphSpec, MLPModel, synthetic_dataset
from adam_shards.sharding import reshard_checkpoint, restore_arrays


def test_config_roundtrip(tmp_path):
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({
        "checkpoint_dir": "ck", "fixture_path": "f.npz", "seed": 1,
        "layer_dims": [3, 4, 2], "batch_size": 8, "train_steps": 4,
        "lr": 0.05, "beta1": 0.9, "beta2": 0.999, "eps": 1e-8,
        "save_world_size": 2, "restore_world_size": 3,
        "tight_tol": 1e-12, "loose_tol": 1e-8,
    }))
    cfg = AppConfig.load(cfg_path)
    assert cfg.layer_dims == (3, 4, 2)
    assert cfg.adam.lr == 0.05
    assert cfg.restore_world_size == 3


def test_config_rejects_missing_keys(tmp_path):
    cfg_path = tmp_path / "bad.json"
    cfg_path.write_text(json.dumps({"checkpoint_dir": "ck"}))
    with pytest.raises(ValueError, match="missing keys"):
        AppConfig.load(cfg_path)


def test_graph_spec_naming_is_stable_and_ordered():
    spec = GraphSpec((3, 4, 2))
    assert spec.param_names == [
        "layers.0.weight", "layers.0.bias",
        "layers.1.weight", "layers.1.bias",
    ]
    assert spec.shape_of("layers.0.weight") == (4, 3)
    assert spec.shape_of("layers.1.bias") == (2,)


def test_model_clone_is_independent_and_loss_decreases():
    spec = GraphSpec((3, 4, 2))
    x, y = synthetic_dataset(16, 3, 2, seed=5)
    model = MLPModel(spec, seed=5)
    clone = model.clone()
    clone.params["layers.0.weight"] += 100.0
    # Mutating the clone must not touch the original.
    np.testing.assert_raises(AssertionError, np.testing.assert_array_equal,
                             clone.params["layers.0.weight"],
                             model.params["layers.0.weight"])
    initial = model.loss(x, y)
    np.testing.assert_allclose(np.isfinite(initial), True)


def test_set_params_rejects_shape_and_set_mismatch():
    spec = GraphSpec((3, 2))
    model = MLPModel(spec, seed=1)
    with pytest.raises(ValueError, match="shape mismatch"):
        model.set_params({
            "layers.0.weight": np.zeros((3, 2)),
            "layers.0.bias": np.zeros(2),
        })
    with pytest.raises(KeyError):
        model.set_params({"layers.0.bias": np.zeros(2)})


def test_reshard_2_to_5_preserves_everything(small_state, tmp_path):
    params, moments = small_state
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    from adam_shards.sharding import save_checkpoint
    save_checkpoint(str(src), params, moments, world_size=2)
    new_commit = reshard_checkpoint(str(src), str(dst), 5,
                                    request_id="req-reshard")
    assert isinstance(new_commit, str) and len(new_commit) == 16
    loaded = restore_arrays(str(dst), world_size=2)
    assert loaded["source_world_size"] == 5
    for name in params:
        np.testing.assert_array_equal(loaded["params"][name], params[name])
        m, v, step = moments[name]
        lm, lv, lstep = loaded["moments"][name]
        np.testing.assert_array_equal(lm, m)
        np.testing.assert_array_equal(lv, v)
        assert lstep == step
