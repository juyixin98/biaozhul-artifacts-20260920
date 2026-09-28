"""Checkpoints: scaler state, counters, weights, and resume identity."""

import json

from mptrainer.checkpoint import load_checkpoint, save_checkpoint
from mptrainer.trainer import DECISION_SKIPPED_OVERFLOW
from mptrainer.validation import params_bitwise_equal, snapshot

from conftest import OVERFLOW_AMPLIFY


def test_checkpoint_persists_scaler_state(trainer, source, tmp_path):
    # Drive the scaler away from its initial value before checkpointing.
    trainer.train_step(*source.batch(16, amplify=OVERFLOW_AMPLIFY))  # 1024 -> 512
    trainer.train_step(*source.batch(16))                            # clean commit
    assert trainer.scaler.scale == 512.0
    assert trainer.scaler.good_steps == 1

    ckpt = save_checkpoint(trainer, tmp_path / "ckpt")
    state = json.loads((ckpt / "state.json").read_text())
    # The scale is first-class checkpoint state, not recomputed on load.
    assert state["scaler"]["scale"] == 512.0
    assert state["scaler"]["good_steps"] == 1
    assert state["micro_step"] == 2
    assert state["optimizer_step"] == 1

    restored = load_checkpoint(ckpt, run_id="restored", log_dir=tmp_path / "logs2")
    assert restored.scaler.scale == 512.0
    assert restored.scaler.good_steps == 1
    assert restored.micro_step == 2
    assert restored.optimizer_step == 1
    assert params_bitwise_equal(trainer.master, restored.master)
    assert params_bitwise_equal(trainer.opt_state, restored.opt_state)


def test_resumed_run_matches_uninterrupted_run(trainer, source, tmp_path):
    # Pre-generate the whole batch sequence so both runs see identical data.
    batches = [source.batch(16) for _ in range(3)]
    batches.insert(1, source.batch(16, amplify=OVERFLOW_AMPLIFY))

    for x, y in batches[:2]:
        trainer.train_step(x, y)
    ckpt = save_checkpoint(trainer, tmp_path / "ckpt")
    resumed = load_checkpoint(ckpt, run_id="resumed", log_dir=tmp_path / "logs2")

    decisions_a, decisions_b = [], []
    for x, y in batches[2:]:
        decisions_a.append(trainer.train_step(x, y).decision)
        decisions_b.append(resumed.train_step(x, y).decision)

    assert decisions_a == decisions_b
    assert params_bitwise_equal(trainer.master, resumed.master)
    assert trainer.scaler.scale == resumed.scaler.scale
    assert trainer.optimizer_step == resumed.optimizer_step


def test_checkpoint_after_overflow_keeps_backed_off_scale(trainer, source, tmp_path):
    trainer.train_step(*source.batch(16, amplify=OVERFLOW_AMPLIFY))
    trainer.train_step(*source.batch(16, amplify=OVERFLOW_AMPLIFY))
    assert trainer.scaler.scale == 256.0
    ckpt = save_checkpoint(trainer, tmp_path / "ckpt")
    restored = load_checkpoint(ckpt, run_id="restored", log_dir=tmp_path / "logs2")
    assert restored.scaler.scale == 256.0
    # And the restored trainer still skips on overflow at the restored scale.
    rec = restored.train_step(*source.batch(16, amplify=OVERFLOW_AMPLIFY))
    assert rec.decision == DECISION_SKIPPED_OVERFLOW
    assert rec.scale_before == 256.0 and rec.scale_after == 128.0


def test_snapshot_is_deep_copy(trainer):
    snap = snapshot(trainer.master)
    trainer.master["W0"][0, 0] += 1.0
    assert not params_bitwise_equal(snap, trainer.master)
