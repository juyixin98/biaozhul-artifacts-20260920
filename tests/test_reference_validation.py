"""Numerical validation against the independent fp64 oracle.

Reference answers here come from ``amptrain.reference.FullPrecisionOracle``
-- a separate fp64 re-implementation -- plus closed-form expectations, never
from the trainer under test.
"""

import numpy as np

from amptrain.config import OptimizerConfig, ScalerConfig
from amptrain.reference import FullPrecisionOracle
from amptrain.trainer import STATUS_COMMITTED, STATUS_SKIPPED, MixedPrecisionTrainer

from tests.helpers import (
    amplified_batches,
    make_config,
    normal_batches,
    snapshot_weights,
)

# fp16 forward introduces relative error ~1e-3 per step; weights are O(0.5)
# and per-step updates O(1e-2), so a 5e-2 relative band is tight enough to
# catch real divergence (e.g. forgotten unscale would be off by 128x).
WEIGHT_RTOL = 5e-2
WEIGHT_ATOL = 5e-3


def _assert_weights_close(trainer, oracle, rtol=WEIGHT_RTOL, atol=WEIGHT_ATOL):
    for name in ("W1", "W2"):
        np.testing.assert_allclose(
            trainer.master.matrices[name].astype(np.float64),
            oracle.w[name],
            rtol=rtol,
            atol=atol,
            err_msg=f"master weight {name} diverged from fp64 oracle",
        )


def test_committed_steps_track_fp64_oracle():
    cfg = make_config()
    trainer = MixedPrecisionTrainer(cfg, run_id="oracle-track")
    oracle = FullPrecisionOracle(cfg, snapshot_weights(trainer))

    windows = [normal_batches(cfg, 1, seed=100 + i) for i in range(6)]
    for batches in windows:
        outcome = trainer.process_window(batches)
        assert outcome.status == STATUS_COMMITTED
        oracle.step(batches)
        _assert_weights_close(trainer, oracle)


def test_skipped_window_leaves_master_bit_identical_and_tracks_oracle():
    cfg = make_config()
    trainer = MixedPrecisionTrainer(cfg, run_id="oracle-skip")
    oracle = FullPrecisionOracle(cfg, snapshot_weights(trainer))

    first = normal_batches(cfg, 1, seed=200)
    trainer.process_window(first)
    oracle.step(first)  # same data, committed
    _assert_weights_close(trainer, oracle)

    pre_skip = snapshot_weights(trainer)
    skip = trainer.process_window(amplified_batches(cfg, 1000.0, 1, seed=201))
    assert skip.status == STATUS_SKIPPED

    # Master-weight conservation is exact: bit-identical before/after skip.
    for name in ("W1", "W2"):
        np.testing.assert_array_equal(
            trainer.master.matrices[name], pre_skip[name]
        )
    # Oracle was not stepped for the skipped window; trainer still matches it.
    _assert_weights_close(trainer, oracle)

    # Recovery: next good window commits and both advance together.
    recovery = normal_batches(cfg, 1, seed=202)
    trainer.process_window(recovery)
    oracle.step(recovery)
    _assert_weights_close(trainer, oracle)


def test_loss_scale_is_numerically_neutral_on_commits():
    """init_scale=1 vs 128 must give (nearly) identical weight trajectories."""
    cfg_lo = make_config(scaler=ScalerConfig(init_scale=1.0))
    cfg_hi = make_config(scaler=ScalerConfig(init_scale=128.0))
    t_lo = MixedPrecisionTrainer(cfg_lo, run_id="scale-1")
    t_hi = MixedPrecisionTrainer(cfg_hi, run_id="scale-128")

    for i in range(4):
        batches = normal_batches(cfg_lo, 1, seed=300 + i)
        t_lo.process_window(batches)
        t_hi.process_window(batches)

    for name in ("W1", "W2"):
        np.testing.assert_allclose(
            t_lo.master.matrices[name], t_hi.master.matrices[name],
            rtol=1e-3, atol=1e-5,
            err_msg=f"loss scale leaked into committed weights ({name})",
        )


def test_training_actually_reduces_loss():
    """Functional check: committed steps reduce loss on a fixed data pool."""
    # lr=0.01 is the stable regime for this task scale; lr=0.05 + momentum
    # diverges even in a pure-fp32 control, so it would test nothing about
    # mixed precision.
    cfg = make_config(optimizer=OptimizerConfig(lr=0.01, momentum=0.9))
    trainer = MixedPrecisionTrainer(cfg, run_id="learn")
    # Fixed pool from ONE shared teacher (a single seed -> one teacher w),
    # reused across epochs; the pool-mean loss is a genuine optimization
    # check rather than per-batch noise from contradictory targets.
    pool = normal_batches(cfg, n=8, seed=400)

    from amptrain.graph import loss_fp32

    def pool_loss():
        return float(np.mean([
            loss_fp32(trainer.master.matrices, x, y, cfg.model.activation)
            for x, y in pool
        ]))

    initial = pool_loss()
    for _ in range(5):  # 5 epochs x 8 windows = 40 committed steps
        for batch in pool:
            assert trainer.process_window([batch]).status == STATUS_COMMITTED
    final = pool_loss()

    assert trainer.counters.committed_steps == 40
    assert final < initial * 0.1, f"loss did not decrease enough: {initial} -> {final}"


def test_lr_progress_matches_oracle_schedule_after_mixed_run():
    cfg = make_config(
        optimizer=OptimizerConfig(lr=0.1, momentum=0.0, schedule="step", step_size=2, gamma=0.5)
    )
    trainer = MixedPrecisionTrainer(cfg, run_id="lr-oracle")
    oracle = FullPrecisionOracle(cfg, snapshot_weights(trainer))

    plan = [
        normal_batches(cfg, 1, seed=501),
        amplified_batches(cfg, 1000.0, 1, seed=502),  # skipped
        normal_batches(cfg, 1, seed=503),
        normal_batches(cfg, 1, seed=504),
        amplified_batches(cfg, 1000.0, 1, seed=505),  # skipped
        normal_batches(cfg, 1, seed=506),
    ]
    for batches in plan:
        outcome = trainer.process_window(batches)
        if outcome.status == STATUS_COMMITTED:
            oracle.step(batches)
        # LR reported for the NEXT step must equal the oracle's schedule,
        # which only counts committed steps.
        assert trainer.scheduler.learning_rate() == oracle.expected_lr()

    assert trainer.counters.committed_steps == 4
    assert trainer.counters.skipped_windows == 2
    # 4 committed steps with step_size=2 -> two drops -> 0.1 * 0.5^2
    assert trainer.scheduler.learning_rate() == 0.025
