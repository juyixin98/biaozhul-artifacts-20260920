"""Gradient accumulation: a window commits atomically or not at all."""

import numpy as np

from mptrainer.config import TrainerConfig
from mptrainer.trainer import (
    DECISION_ACCUMULATING,
    DECISION_COMMITTED,
    DECISION_SKIPPED_OVERFLOW,
    MixedPrecisionTrainer,
)
from mptrainer.validation import params_bitwise_equal, snapshot

from conftest import OVERFLOW_AMPLIFY


def make_trainer(accum_steps: int, tmp_path, lr_decay: float = 0.05) -> MixedPrecisionTrainer:
    config = TrainerConfig(
        layer_sizes=[8, 16, 1], seed=1234, base_lr=0.01, lr_decay=lr_decay,
        momentum=0.9, accum_steps=accum_steps, low_dtype="float16",
        init_scale=1024.0, growth_interval=4,
    ).validate()
    return MixedPrecisionTrainer(config, run_id="test-accum", log_dir=tmp_path / "logs")


def test_window_commits_only_when_full(tmp_path, source):
    trainer = make_trainer(accum_steps=3, tmp_path=tmp_path)
    before = snapshot(trainer.master)

    r1 = trainer.train_step(*source.batch(16))
    r2 = trainer.train_step(*source.batch(16))
    assert r1.decision == DECISION_ACCUMULATING
    assert r2.decision == DECISION_ACCUMULATING
    assert trainer.optimizer_step == 0
    # No commit yet: master weights untouched while the window fills.
    assert params_bitwise_equal(before, trainer.master)

    r3 = trainer.train_step(*source.batch(16))
    assert r3.decision == DECISION_COMMITTED
    assert trainer.optimizer_step == 1
    assert not params_bitwise_equal(before, trainer.master)


def test_overflow_anywhere_in_window_discards_everything(tmp_path, source):
    trainer = make_trainer(accum_steps=3, tmp_path=tmp_path)
    before = snapshot(trainer.master)

    trainer.train_step(*source.batch(16))                        # window 1/3
    trainer.train_step(*source.batch(16))                        # window 2/3
    rec = trainer.train_step(*source.batch(16, amplify=OVERFLOW_AMPLIFY))

    assert rec.decision == DECISION_SKIPPED_OVERFLOW
    assert "discarded 2 accumulated micro-step(s)" in rec.reason
    # Atomicity: the two clean micro-steps are NOT partially committed.
    assert trainer.optimizer_step == 0
    assert params_bitwise_equal(before, trainer.master)
    assert trainer.state_summary()["window_position"] == 0


def test_lr_progress_unchanged_by_poisoned_window(tmp_path, source):
    """Skipped windows must not advance the LR schedule."""
    trainer = make_trainer(accum_steps=2, tmp_path=tmp_path)
    clean = make_trainer(accum_steps=2, tmp_path=tmp_path / "clean")

    # Poisoned run: commit one window, hit overflow, commit another window.
    trainer.train_step(*source.batch(16))
    trainer.train_step(*source.batch(16))                        # opt_step=1
    trainer.train_step(*source.batch(16, amplify=OVERFLOW_AMPLIFY))
    trainer.train_step(*source.batch(16))
    trainer.train_step(*source.batch(16))                        # opt_step=2

    # Clean run: two windows, no overflow.
    for _ in range(4):
        clean.train_step(*source.batch(16))                      # opt_step=2

    assert trainer.optimizer_step == clean.optimizer_step == 2
    assert trainer.lr() == clean.lr()
    # The overflow did cost wall-clock micro-steps, and the logs show it.
    assert trainer.micro_step == 5
    assert clean.micro_step == 4


def test_accumulated_gradient_is_window_mean(tmp_path):
    """Committed gradient equals the mean of the window's per-step grads.

    Reference computed by hand in fp64 for a linear model (momentum=0).
    """
    config = TrainerConfig(
        layer_sizes=[3, 1], seed=99, base_lr=0.1, lr_decay=0.0,
        momentum=0.0, accum_steps=2, low_dtype="float32",
        init_scale=4.0, growth_interval=10**9,
    ).validate()
    trainer = MixedPrecisionTrainer(config, run_id="test-accum-mean")
    w0 = trainer.master["W0"].astype(np.float64)
    b0 = trainer.master["b0"].astype(np.float64)

    rng = np.random.default_rng(21)
    batches = [
        (rng.standard_normal((4, 3)), rng.standard_normal((4, 1)))
        for _ in range(2)
    ]
    for x, y in batches:
        trainer.train_step(x.astype(np.float32), y.astype(np.float32))

    grads = []
    for x, y in batches:
        diff = x @ w0 + b0 - y
        grads.append(((2.0 / 4) * (x.T @ diff), (2.0 / 4) * diff.sum(axis=0)))
    mean_gw = (grads[0][0] + grads[1][0]) / 2.0
    mean_gb = (grads[0][1] + grads[1][1]) / 2.0

    np.testing.assert_allclose(
        trainer.master["W0"].astype(np.float64), w0 - 0.1 * mean_gw, rtol=1e-5
    )
    np.testing.assert_allclose(
        trainer.master["b0"].astype(np.float64), b0 - 0.1 * mean_gb, rtol=1e-5
    )
