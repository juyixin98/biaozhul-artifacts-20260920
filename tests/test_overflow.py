"""Overflow semantics: skip the whole update, back off the scale, keep masters."""

import json

from mptrainer.trainer import DECISION_COMMITTED, DECISION_SKIPPED_OVERFLOW
from mptrainer.validation import params_bitwise_equal, snapshot

from conftest import OVERFLOW_AMPLIFY


def test_amplified_input_triggers_overflow_skip(trainer, source):
    # One clean commit first so we know the pipeline commits when healthy.
    rec = trainer.train_step(*source.batch(16))
    assert rec.decision == DECISION_COMMITTED
    assert trainer.optimizer_step == 1

    before = snapshot(trainer.master)
    opt_before = trainer.optimizer_step
    micro_before = trainer.micro_step
    scale_before = trainer.scaler.scale

    x, y = source.batch(16, amplify=OVERFLOW_AMPLIFY)
    rec = trainer.train_step(x, y)

    assert rec.decision == DECISION_SKIPPED_OVERFLOW
    assert rec.overflow is True
    assert rec.loss is None
    # Master weights bitwise conserved: the rejected update left no trace.
    assert params_bitwise_equal(before, trainer.master)
    # Step semantics: optimizer_step frozen, micro_step advanced.
    assert trainer.optimizer_step == opt_before
    assert trainer.micro_step == micro_before + 1
    # Scale backed off by the configured factor.
    assert trainer.scaler.scale == scale_before * trainer.config.backoff_factor
    assert rec.scale_after == trainer.scaler.scale


def test_recovery_after_overflow_matches_uninterrupted_lr(trainer, source):
    """LR schedule is a pure function of optimizer_step: skips don't move it."""
    trainer.train_step(*source.batch(16))            # commit 1
    trainer.train_step(*source.batch(16, amplify=OVERFLOW_AMPLIFY))  # skipped
    lr_after_skip = trainer.lr()
    trainer.train_step(*source.batch(16))            # commit 2
    assert trainer.optimizer_step == 2
    # At optimizer_step==1 the LR must equal the schedule evaluated at 1.
    expected = trainer.config.base_lr / (1.0 + trainer.config.lr_decay * 1)
    assert lr_after_skip == expected


def test_repeated_overflows_keep_backing_off(trainer, source):
    scales = []
    for _ in range(3):
        rec = trainer.train_step(*source.batch(16, amplify=OVERFLOW_AMPLIFY))
        scales.append(rec.scale_after)
    assert scales == [512.0, 256.0, 128.0]
    assert trainer.optimizer_step == 0


def test_run_log_correlates_decision_with_run_and_versions(trainer, source, tmp_path):
    trainer.train_step(*source.batch(16, amplify=OVERFLOW_AMPLIFY))

    log_file = tmp_path / "logs" / "test-run.jsonl"
    lines = [json.loads(line) for line in log_file.read_text().splitlines()]
    step_records = [r for r in lines if r["event"] == "train_step"]
    assert len(step_records) == 1
    rec = step_records[0]
    # Identity, progress, and decision basis are all present in the log.
    assert rec["run_id"] == "test-run"
    assert rec["decision"] == DECISION_SKIPPED_OVERFLOW
    assert rec["overflow"] is True
    assert rec["micro_step"] == 1
    assert rec["scale_before"] == 1024.0 and rec["scale_after"] == 512.0
    assert "discarded" in rec["reason"]
    assert rec["versions"]["numpy"]
    assert rec["versions"]["mptrainer"]
