"""Layer 4: sparse transactional persistence."""

from __future__ import annotations

import json

import numpy as np
import pytest

from sparse_embedding.config import ClipMode, OptimizerConfig, TableSpec
from sparse_embedding.errors import PersistenceError, StateShapeError
from sparse_embedding.persistence import (
    DATA_FILENAME,
    MANIFEST_FILENAME,
    CheckpointStore,
)
from sparse_embedding.state import TrainingState
from sparse_embedding.tensors import SparseGradientBatch


def state_with_updates(tmp_path, *, num_rows=100, dim=4, n_batches=2):
    spec = TableSpec(num_rows=num_rows, dim=dim)
    state = TrainingState(spec, seed=11)
    opt = OptimizerConfig(lr=0.1, momentum=0.9)
    from sparse_embedding.config import ClipConfig

    clip = ClipConfig(ClipMode.NONE)
    for b in range(n_batches):
        # Only a handful of rows are ever touched.
        idx = [2, 5 + b]
        vals = np.full((2, dim), float(b + 1))
        batch = SparseGradientBatch.from_pairs(idx, vals, spec)
        state.apply_batch(batch, opt, clip, run_id=f"seed-{b}")
    return state


def make_cfg(tmp_path, num_rows=100, dim=4):
    from sparse_embedding.config import ClipConfig, OptimizerConfig, ServiceConfig

    return ServiceConfig(
        table=TableSpec(num_rows=num_rows, dim=dim),
        optimizer=OptimizerConfig(lr=0.1, momentum=0.9),
        clip=ClipConfig(ClipMode.NONE),
        state_dir=str(tmp_path / "state"),
        seed=11,
    )


def test_checkpoint_persists_only_touched_rows(tmp_path):
    cfg = make_cfg(tmp_path)
    state = state_with_updates(tmp_path, num_rows=100, dim=4)
    store = CheckpointStore(cfg.state_dir)
    manifest = store.save(state, cfg)

    assert manifest.n_touched == len(state.touched_rows)
    assert manifest.n_touched < 100  # sparse: far from the full table

    with np.load(store.data_path) as data:
        saved_indices = data["indices"].tolist()
    assert sorted(saved_indices) == sorted(state.touched_rows)


def test_round_trip_restores_touched_and_frozen_untouched_rows(tmp_path):
    cfg = make_cfg(tmp_path)
    state = state_with_updates(tmp_path)
    touched = sorted(state.touched_rows)
    store = CheckpointStore(cfg.state_dir)
    store.save(state, cfg)

    restored = store.load(cfg)
    np.testing.assert_allclose(restored.weights, state.weights)
    np.testing.assert_allclose(restored.momentum, state.momentum)
    np.testing.assert_array_equal(restored.row_steps, state.row_steps)
    assert restored.global_step == state.global_step
    assert restored.touched_rows == frozenset(touched)

    # Untouched rows must equal fresh deterministic init (never serialised).
    fresh = TrainingState(cfg.table, seed=cfg.seed)
    for r in range(cfg.table.num_rows):
        if r not in touched:
            np.testing.assert_array_equal(restored.weights[r], fresh.weights[r])
            assert restored.row_steps[r] == 0


def test_loading_then_more_updates_keeps_momentum_history(tmp_path):
    cfg = make_cfg(tmp_path)
    state = state_with_updates(tmp_path)
    store = CheckpointStore(cfg.state_dir)
    store.save(state, cfg)
    restored = store.load(cfg)

    # Momentum on a touched row is non-zero and survives the restart.
    assert not np.allclose(restored.momentum[2], 0.0)
    opt = cfg.optimizer
    from sparse_embedding.config import ClipConfig

    batch = SparseGradientBatch.from_pairs([2], np.ones((1, 4)), cfg.table)
    report = restored.apply_batch(batch, opt, ClipConfig(ClipMode.NONE), run_id="post-load")
    # Counter continues from the restored value, not from zero.
    assert report.row_steps_after[0] == restored.row_steps[2]
    assert restored.row_steps[2] == state.row_steps[2] + 1


def test_save_is_atomic_no_tmp_left_behind(tmp_path):
    cfg = make_cfg(tmp_path)
    state = state_with_updates(tmp_path)
    store = CheckpointStore(cfg.state_dir)
    store.save(state, cfg)
    leftovers = [p for p in __import__("pathlib").Path(cfg.state_dir).iterdir()
                 if p.name.endswith(".tmp")]
    assert leftovers == []


def test_save_failure_rolls_back_and_keeps_previous_checkpoint(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    state = state_with_updates(tmp_path)
    store = CheckpointStore(cfg.state_dir)
    store.save(state, cfg)
    good_manifest = json.loads((tmp_path / "state" / MANIFEST_FILENAME).read_text())

    # Force the publish of the data file to fail; the old checkpoint survives.
    real_replace = __import__("os").replace

    def flaky(src, dst):
        if dst.endswith(DATA_FILENAME):
            raise OSError("simulated publish failure")
        return real_replace(src, dst)

    monkeypatch.setattr("sparse_embedding.persistence.os.replace", flaky)
    with pytest.raises(PersistenceError) as exc:
        store.save(state, cfg)
    assert exc.value.code == "persistence_error"

    still = json.loads((tmp_path / "state" / MANIFEST_FILENAME).read_text())
    assert still == good_manifest
    # No temp artefacts remain after rollback.
    leftovers = list(__import__("pathlib").Path(cfg.state_dir).glob(".*.tmp"))
    assert leftovers == []


def test_load_rejects_incompatible_table_shape(tmp_path):
    cfg = make_cfg(tmp_path, num_rows=100, dim=4)
    state = state_with_updates(tmp_path, num_rows=100, dim=4)
    store = CheckpointStore(cfg.state_dir)
    store.save(state, cfg)

    # Different declared table: must refuse rather than silently misalign.
    bad_cfg = make_cfg(tmp_path, num_rows=50, dim=4)
    bad_cfg = ServiceConfig_with_dir(bad_cfg, cfg.state_dir)
    with pytest.raises(StateShapeError) as exc:
        store.load(bad_cfg)
    assert exc.value.code == "state_shape_error"


def test_load_rejects_incompatible_dim(tmp_path):
    cfg = make_cfg(tmp_path, num_rows=100, dim=4)
    state = state_with_updates(tmp_path, num_rows=100, dim=4)
    store = CheckpointStore(cfg.state_dir)
    store.save(state, cfg)

    bad_cfg = make_cfg(tmp_path, num_rows=100, dim=9)
    bad_cfg = ServiceConfig_with_dir(bad_cfg, cfg.state_dir)
    with pytest.raises(StateShapeError):
        store.load(bad_cfg)


def test_load_missing_checkpoint_is_persistence_error(tmp_path):
    cfg = make_cfg(tmp_path)
    store = CheckpointStore(str(tmp_path / "does-not-exist"))
    with pytest.raises(PersistenceError):
        store.load(cfg)


# --- helpers to build an alternate config pointing at an existing state dir ---

from sparse_embedding.config import ServiceConfig  # noqa: E402


def ServiceConfig_with_dir(cfg: ServiceConfig, state_dir: str) -> ServiceConfig:
    return ServiceConfig(
        table=cfg.table,
        optimizer=cfg.optimizer,
        clip=cfg.clip,
        state_dir=state_dir,
        seed=cfg.seed,
    )
