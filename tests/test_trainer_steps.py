"""Trainer step semantics: overflow skip, atomic accumulation, recovery."""

import numpy as np
import pytest

from amptrain.config import (
    AccumulationConfig,
    OptimizerConfig,
    PrecisionConfig,
    ScalerConfig,
)
from amptrain.errors import AmpTrainError, ErrorCode
from amptrain.trainer import STATUS_COMMITTED, STATUS_SKIPPED, MixedPrecisionTrainer

from tests.helpers import (
    amplified_batches,
    make_config,
    normal_batches,
    snapshot_momentum,
    snapshot_weights,
    weight_dicts_equal,
)

AMP = 1000.0  # calibrated: overflows fp16 at the scaled-loss stage, finite in fp32


def test_normal_window_commits_and_advances_counters(trainer):
    before = snapshot_weights(trainer)
    outcome = trainer.process_window(normal_batches(trainer.config, 1))
    assert outcome.status == STATUS_COMMITTED
    assert outcome.committed_steps == 1
    assert outcome.skipped_windows == 0
    assert outcome.grad_norm is not None and outcome.grad_norm > 0
    assert not weight_dicts_equal(snapshot_weights(trainer), before)


def test_overflow_skips_window_and_backs_off_scale(trainer):
    before_w = snapshot_weights(trainer)
    before_m = snapshot_momentum(trainer)
    scale_before = trainer.scaler.scale

    outcome = trainer.process_window(amplified_batches(trainer.config, AMP, 1))

    assert outcome.status == STATUS_SKIPPED
    assert outcome.overflow_stage == "scaled_loss"
    assert outcome.scale_before == scale_before == 128.0
    assert outcome.scale_after == 64.0
    # master weights and momentum are bit-identical: nothing was committed
    assert weight_dicts_equal(snapshot_weights(trainer), before_w)
    assert weight_dicts_equal(snapshot_momentum(trainer), before_m)
    assert trainer.counters.committed_steps == 0
    assert trainer.counters.skipped_windows == 1


def test_skipped_window_does_not_advance_lr_schedule():
    cfg = make_config(
        optimizer=OptimizerConfig(lr=0.1, momentum=0.0, schedule="step", step_size=2, gamma=0.5)
    )
    trainer = MixedPrecisionTrainer(cfg, run_id="lr-test")
    # commit 2 windows -> lr should drop to 0.05 for the 3rd commit
    trainer.process_window(normal_batches(cfg, 1, seed=1))
    trainer.process_window(normal_batches(cfg, 1, seed=2))
    assert trainer.scheduler.learning_rate() == 0.05
    # a skipped window must not consume schedule progress
    skip = trainer.process_window(amplified_batches(cfg, AMP, 1, seed=3))
    assert skip.status == STATUS_SKIPPED
    assert trainer.scheduler.committed_steps == 2
    # next commit uses the lr of committed step #3 (index 2 -> 0.05)
    outcome = trainer.process_window(normal_batches(cfg, 1, seed=4))
    assert outcome.status == STATUS_COMMITTED
    assert outcome.lr == 0.05
    assert trainer.scheduler.committed_steps == 3


def test_recovery_after_overflow_continues_training(trainer):
    trainer.process_window(amplified_batches(trainer.config, AMP, 1))
    assert trainer.scaler.scale == 64.0
    outcome = trainer.process_window(normal_batches(trainer.config, 1))
    assert outcome.status == STATUS_COMMITTED
    assert trainer.counters.committed_steps == 1
    assert trainer.counters.skipped_windows == 1
    assert trainer.counters.window_index == 2


def test_accumulation_window_is_atomic_on_any_overflow():
    cfg = make_config(accumulation=AccumulationConfig(micro_batches=3))
    trainer = MixedPrecisionTrainer(cfg, run_id="accum-test")
    before_w = snapshot_weights(trainer)
    before_m = snapshot_momentum(trainer)

    good1 = normal_batches(cfg, 1, seed=11)[0]
    bad = amplified_batches(cfg, AMP, 1, seed=12)[0]
    good2 = normal_batches(cfg, 1, seed=13)[0]
    outcome = trainer.process_window([good1, bad, good2])

    assert outcome.status == STATUS_SKIPPED
    assert outcome.overflow_micro_index == 1  # overflow located at micro-batch 2
    # No partial commit: even the two good micro-batches are discarded.
    assert weight_dicts_equal(snapshot_weights(trainer), before_w)
    assert weight_dicts_equal(snapshot_momentum(trainer), before_m)
    assert trainer.counters.committed_steps == 0
    assert trainer.counters.skipped_windows == 1
    assert trainer.scaler.scale == 64.0


def test_accumulation_window_commits_all_micro_batches_together():
    cfg = make_config(accumulation=AccumulationConfig(micro_batches=4))
    trainer = MixedPrecisionTrainer(cfg, run_id="accum-ok")
    outcome = trainer.process_window(normal_batches(cfg, 4, seed=21))
    assert outcome.status == STATUS_COMMITTED
    assert outcome.micro_batches == 4
    assert trainer.counters.committed_steps == 1  # one optimizer step per window


def test_wrong_micro_batch_count_is_rejected(trainer):
    with pytest.raises(AmpTrainError) as excinfo:
        trainer.process_window(normal_batches(trainer.config, 2))
    assert excinfo.value.code == ErrorCode.INPUT_INVALID


def test_non_finite_input_is_rejected_not_silently_skipped(trainer):
    x, y = normal_batches(trainer.config, 1)[0]
    x = x.copy()
    x[0, 0] = np.inf
    with pytest.raises(AmpTrainError) as excinfo:
        trainer.process_window([(x, y)])
    assert excinfo.value.code == ErrorCode.INPUT_INVALID


def test_scale_grows_after_growth_interval_of_commits():
    cfg = make_config(scaler=ScalerConfig(init_scale=4.0, growth_interval=3))
    trainer = MixedPrecisionTrainer(cfg, run_id="grow-test")
    for _ in range(3):
        trainer.process_window(normal_batches(cfg, 1, seed=31))
    assert trainer.scaler.scale == 8.0
    # a skip resets the streak
    trainer.process_window(amplified_batches(cfg, AMP, 1, seed=32))
    assert trainer.scaler.scale == 4.0
    assert trainer.scaler.growth_tracker == 0


def test_fp32_control_classifies_amplified_window_as_committable_while_fp16_skips():
    """Decision contrast on identical inputs, plus uninterrupted fp16 recovery.

    The fp32 control's role is to prove the overflow is a *low-precision range*
    phenomenon: the very same amplified window is finite in fp32 and therefore
    committed there, but classified as overflow in fp16 and skipped.  It is
    NOT a claim that fp32 training is healthy on numerically adversarial input
    (the true unscaled gradient is large there; that update diverges -- which
    is exactly why the protective skip/backoff is observable).  What we assert:

    * per-window verdicts differ only at the amplified window (skip vs commit);
    * right after that window fp16 master weights are conserved while fp32
      actually moved (it applied the large update);
    * the fp16 run is uninterrupted afterwards: backoff then commits resume.
    """
    cfg_fp32 = make_config(precision=PrecisionConfig(lowp_dtype="float32"))
    cfg_fp16 = make_config(precision=PrecisionConfig(lowp_dtype="float16"))
    t32 = MixedPrecisionTrainer(cfg_fp32, run_id="ctrl-fp32")
    t16 = MixedPrecisionTrainer(cfg_fp16, run_id="ctrl-fp16")

    good1 = normal_batches(cfg_fp16, 1, seed=41)[0]
    good2 = normal_batches(cfg_fp16, 1, seed=42)[0]
    bad = amplified_batches(cfg_fp16, AMP, 1, seed=43)[0]

    # Both runs start identically on the two healthy windows.
    for b in (good1, good2):
        assert t32.process_window([b]).status == STATUS_COMMITTED
        assert t16.process_window([b]).status == STATUS_COMMITTED

    w16_before = snapshot_weights(t16)
    w32_before = snapshot_weights(t32)
    o32 = t32.process_window([bad])
    o16 = t16.process_window([bad])

    # Decision contrast at the same amplified window.
    assert o32.status == STATUS_COMMITTED
    assert o16.status == STATUS_SKIPPED
    assert o16.overflow_stage == "scaled_loss"

    # fp16 conserved master weights; fp32 actually applied the (large) update.
    assert weight_dicts_equal(snapshot_weights(t16), w16_before)
    fp32_moved = any(
        not np.array_equal(snapshot_weights(t32)[n], w32_before[n]) for n in ("W1", "W2")
    )
    assert fp32_moved

    # fp16 run is uninterrupted after backoff: subsequent healthy windows commit
    # and committed-step / LR progress continue from where they were.
    assert t16.scaler.scale == 64.0
    rec1 = t16.process_window([normal_batches(cfg_fp16, 1, seed=44)[0]])
    rec2 = t16.process_window([normal_batches(cfg_fp16, 1, seed=45)[0]])
    assert rec1.status == rec2.status == STATUS_COMMITTED
    assert t16.counters.committed_steps == 4
    assert t16.counters.skipped_windows == 1
    assert t16.scheduler.committed_steps == 4


def test_dynamic_loss_scaling_classic_scenario_fp32_uninterrupted_fp16_recovers():
    """Healthy inputs + an over-large scale: the textbook dynamic scaling loop.

    The *true* gradients are healthy (proven by fp32 converging 5.0 -> 0.14);
    the only reason fp16 skips is that the scaled values leave fp16 range.
    fp32 commits every window uninterrupted, while fp16 skips, backs the scale
    off monotonically, and resumes committing once the scale fits.
    """
    big_scale = ScalerConfig(init_scale=32768.0, growth_interval=10**9)
    cfg32 = make_config(
        precision=PrecisionConfig(lowp_dtype="float32"),
        optimizer=OptimizerConfig(lr=0.01, momentum=0.9),
        scaler=big_scale,
    )
    cfg16 = make_config(
        precision=PrecisionConfig(lowp_dtype="float16"),
        optimizer=OptimizerConfig(lr=0.01, momentum=0.9),
        scaler=big_scale,
    )
    t32 = MixedPrecisionTrainer(cfg32, run_id="classic-fp32")
    t16 = MixedPrecisionTrainer(cfg16, run_id="classic-fp16")

    # One shared teacher (single-seed pool), trained for 2 epochs in both runs.
    pool = normal_batches(cfg32, n=8, seed=9)
    from amptrain.graph import loss_fp32

    def pool_loss(trainer, cfg):
        return float(np.mean(
            [loss_fp32(trainer.master.matrices, x, y, cfg.model.activation) for x, y in pool]
        ))

    loss0_32, loss0_16 = pool_loss(t32, cfg32), pool_loss(t16, cfg16)
    out16, out32 = [], []
    for _ in range(2):
        for batch in pool:
            out16.append(t16.process_window([batch]))
            out32.append(t32.process_window([batch]))

    # fp32: uninterrupted, scale untouched, every window commits, and it learns.
    assert all(o.status == STATUS_COMMITTED for o in out32)
    assert t32.scaler.scale == 32768.0
    assert t32.counters.skipped_windows == 0
    assert pool_loss(t32, cfg32) < loss0_32 * 0.5

    # fp16: range-induced skips with strictly monotone backoff, then a return
    # to committing once the scale fits -- and it still learns overall.
    assert any(o.status == STATUS_SKIPPED for o in out16)
    skipped_scales = [o.scale_after for o in out16 if o.status == STATUS_SKIPPED]
    assert skipped_scales == sorted(skipped_scales, reverse=True)  # monotone backoff
    assert skipped_scales[0] < 32768.0
    assert out16[-1].status == STATUS_COMMITTED  # recovered
    assert t16.scaler.scale < 32768.0
    assert pool_loss(t16, cfg16) < loss0_16 * 0.5
