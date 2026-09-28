"""Checkpoint persistence: scaler state, LR progress, corruption detection."""

import json
from pathlib import Path

import numpy as np
import pytest

from amptrain.checkpoint import load_checkpoint, save_checkpoint
from amptrain.errors import AmpTrainError, ErrorCode
from amptrain.trainer import MixedPrecisionTrainer

from tests.helpers import (
    amplified_batches,
    make_config,
    normal_batches,
    snapshot_weights,
    weight_dicts_equal,
)


def _drive(trainer, n_good=3, skip=True):
    for i in range(n_good):
        trainer.process_window(normal_batches(trainer.config, 1, seed=600 + i))
    if skip:
        trainer.process_window(amplified_batches(trainer.config, 1000.0, 1, seed=699))


def test_checkpoint_saves_and_restores_full_state(trainer, tmp_path):
    _drive(trainer, n_good=3, skip=True)
    ckpt = tmp_path / "run-a"
    save_checkpoint(trainer, ckpt)

    restored = load_checkpoint(ckpt)
    assert restored.run_id == trainer.run_id
    assert weight_dicts_equal(snapshot_weights(restored), snapshot_weights(trainer))
    for name in ("W1", "W2"):
        np.testing.assert_array_equal(
            restored.opt_state.momentum[name], trainer.opt_state.momentum[name]
        )
    # Scaler state (the required part): backoff applied before save persists.
    assert restored.scaler.scale == trainer.scaler.scale == 64.0
    assert restored.scaler.growth_tracker == trainer.scaler.growth_tracker == 0
    # Counters and LR progress persist too.
    assert restored.counters == trainer.counters
    assert restored.scheduler.committed_steps == 3


def test_resume_continues_lr_schedule_from_committed_progress(tmp_path):
    cfg = make_config()
    trainer = MixedPrecisionTrainer(cfg, run_id="lr-ckpt")
    _drive(trainer, n_good=2, skip=False)
    ckpt = tmp_path / "run-lr"
    save_checkpoint(trainer, ckpt)

    restored = load_checkpoint(ckpt)
    batches = normal_batches(cfg, 1, seed=701)
    outcome = restored.process_window(batches)
    assert outcome.committed_steps == 3  # continues, does not restart at 0


def test_checkpoint_files_include_explicit_scaler_block(trainer, tmp_path):
    save_checkpoint(trainer, tmp_path / "meta-check")
    meta = json.loads((tmp_path / "meta-check" / "meta.json").read_text())
    assert "scaler" in meta and "scale" in meta["scaler"]
    assert "growth_tracker" in meta["scaler"]
    assert meta["scaler"]["scale"] == 128.0


def test_missing_checkpoint_raises_named_error(tmp_path):
    with pytest.raises(AmpTrainError) as excinfo:
        load_checkpoint(tmp_path / "does-not-exist")
    assert excinfo.value.code == ErrorCode.CHECKPOINT_NOT_FOUND


def test_corrupt_meta_raises_checkpoint_corrupt(tmp_path):
    ckpt = tmp_path / "bad"
    ckpt.mkdir()
    (ckpt / "meta.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(AmpTrainError) as excinfo:
        load_checkpoint(ckpt)
    assert excinfo.value.code == ErrorCode.CHECKPOINT_CORRUPT


def test_non_positive_saved_scale_is_corruption(trainer, tmp_path):
    ckpt = tmp_path / "bad-scale"
    save_checkpoint(trainer, ckpt)
    meta_path = ckpt / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["scaler"]["scale"] = 0.0
    meta_path.write_text(json.dumps(meta))
    with pytest.raises(AmpTrainError) as excinfo:
        load_checkpoint(ckpt)
    assert excinfo.value.code == ErrorCode.CHECKPOINT_CORRUPT


def test_shape_mismatch_is_detected(trainer, tmp_path):
    ckpt = tmp_path / "shape"
    save_checkpoint(trainer, ckpt)
    meta_path = ckpt / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["config"]["model"]["hidden_dim"] = 99
    meta_path.write_text(json.dumps(meta))
    with pytest.raises(AmpTrainError) as excinfo:
        load_checkpoint(ckpt)
    assert excinfo.value.code == ErrorCode.CHECKPOINT_CORRUPT
