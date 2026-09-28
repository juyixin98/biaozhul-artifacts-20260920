"""Real multi-process tests: worker OS processes + uvicorn over HTTP.

These exercise the full stack: each worker is a spawned process computing
on its own shard, talking HTTP to the FastAPI control plane, while the
driver drives synchronous rounds.  Expected results are re-derived with
the independent reference oracle (:mod:`bucket_sync.reference`).
"""

from __future__ import annotations

import numpy as np
import pytest

from bucket_sync.bucketing import BucketLayout
from bucket_sync.config import (
    RuntimeConfig,
    initial_params,
    make_graph,
    make_shards,
)
from bucket_sync.coordinator import Coordinator
from bucket_sync.diagnostics import Diagnostics
from bucket_sync.reference import (
    joint_linear_mse,
    reference_sgd_step,
    reference_run,
    reference_weighted_mean,
)
from bucket_sync.runtime import LocalRuntime, WorkerSpec
from bucket_sync.training import BIAS_PARAM, WEIGHT_PARAM, ModelState

pytestmark = pytest.mark.integration

WORKERS = ["w0", "w1", "w2"]
SIZES = (5, 3, 2)
TOL = 1e-9


def _build_runtime(specs, cfg, *, allow_partial=False, round_timeout=None):
    graph = make_graph(cfg.in_features)
    layout = BucketLayout(graph, cfg.bucket_size)
    model = ModelState(graph, initial_params(cfg.in_features))
    coordinator = Coordinator(
        graph,
        layout,
        model,
        lr=cfg.lr,
        diagnostics=Diagnostics(),
        heartbeat_timeout=cfg.heartbeat_timeout_s,
        allow_partial_coverage=allow_partial,
    )
    return LocalRuntime(
        coordinator,
        layout,
        specs,
        round_timeout_s=round_timeout or cfg.round_timeout_s,
    )


def _specs(shards, **overrides_by_worker):
    specs = []
    for wid, shard in zip(WORKERS, shards):
        kw = {"submit_order_seed": 100 + WORKERS.index(wid) * 7}
        kw.update(overrides_by_worker.get(wid, {}))
        specs.append(
            WorkerSpec(
                worker_id=wid, x_shard=shard[0], y_shard=shard[1], **kw
            )
        )
    return specs


def test_happy_path_two_rounds_matches_reference_run():
    cfg = RuntimeConfig()
    shards = make_shards(cfg.in_features, SIZES, seed=42)
    runtime = _build_runtime(_specs(shards), cfg)
    result = runtime.run(rounds=2)

    assert [o.outcome for o in result.rounds] == ["committed", "committed"]
    assert result.generation == 2
    expected = reference_run(initial_params(cfg.in_features), shards, cfg.lr, rounds=2)
    np.testing.assert_allclose(result.final_params[WEIGHT_PARAM], expected[WEIGHT_PARAM], atol=TOL)
    np.testing.assert_allclose(result.final_params[BIAS_PARAM], expected[BIAS_PARAM], atol=TOL)

    # Evidence: both rounds, generations 0 and 1, true unequal sample basis.
    committed = [e for e in result.diagnostics_events if e["reason"] == "round_committed"]
    gens = sorted(e["detail"]["generation_before"] for e in committed)
    assert gens == [0, 1]
    assert committed[0]["detail"]["samples_per_worker"] == {"w0": 5, "w1": 3, "w2": 2}


def test_shuffled_submission_orders_still_match_reference():
    cfg = RuntimeConfig()
    shards = make_shards(cfg.in_features, SIZES, seed=42)
    specs = _specs(
        shards,
        w0={"submit_order_seed": 1},
        w1={"submit_order_seed": 999},
        w2={"submit_order_seed": 31_337},
    )
    runtime = _build_runtime(specs, cfg)
    result = runtime.run(rounds=1)
    expected = reference_run(initial_params(cfg.in_features), shards, cfg.lr, rounds=1)
    assert result.rounds[0].outcome == "committed"
    np.testing.assert_allclose(result.final_params[WEIGHT_PARAM], expected[WEIGHT_PARAM], atol=TOL)
    np.testing.assert_allclose(result.final_params[BIAS_PARAM], expected[BIAS_PARAM], atol=TOL)


def test_worker_crash_aborts_round_then_survivors_continue_correctly():
    cfg = RuntimeConfig()
    shards = make_shards(cfg.in_features, SIZES, seed=42)
    # w1 crashes after submitting one bucket of round 1.
    specs = _specs(shards, w1={"crash_after_buckets": 1})
    runtime = _build_runtime(specs, cfg)
    result = runtime.run(rounds=2)

    first, second = result.rounds
    assert first.outcome == "aborted"
    assert first.reason == "worker_lost"
    assert second.outcome == "committed"
    assert result.generation == 1  # the aborted round applied no update

    # Round 2 ran over the two survivors (w0: 5 samples, w2: 2 samples).
    surviving = [shards[0], shards[2]]
    expected = reference_run(initial_params(cfg.in_features), surviving, cfg.lr, rounds=1)
    np.testing.assert_allclose(result.final_params[WEIGHT_PARAM], expected[WEIGHT_PARAM], atol=TOL)
    np.testing.assert_allclose(result.final_params[BIAS_PARAM], expected[BIAS_PARAM], atol=TOL)

    reasons = {e["reason"] for e in result.diagnostics_events}
    assert "worker_lost" in reasons
    assert "round_aborted" in reasons
    # w1's own report shows it deliberately crashed mid-round.
    assert result.worker_reports["w1"][0]["crashed"] is True


def test_hung_worker_is_detected_via_missing_heartbeats():
    cfg = RuntimeConfig()
    shards = make_shards(cfg.in_features, SIZES, seed=42)
    specs = _specs(shards, w2={"hang_after_buckets": 1})
    runtime = _build_runtime(specs, cfg)
    result = runtime.run(rounds=1)
    assert result.rounds[0].outcome == "aborted"
    assert result.rounds[0].reason == "worker_lost"
    assert result.generation == 0
    lost_events = [
        e for e in result.diagnostics_events
        if e["reason"] == "worker_lost" and e["worker_id"] == "w2"
    ]
    assert lost_events and lost_events[0]["detail"]["why"] == "heartbeat_timeout"


def test_missing_gradient_strict_mode_never_commits():
    cfg = RuntimeConfig()
    shards = make_shards(cfg.in_features, SIZES, seed=42)
    specs = _specs(shards, w1={"missing_params": (BIAS_PARAM,)})
    # Short timeout: strict mode must keep the round undecided, never apply.
    runtime = _build_runtime(specs, cfg, allow_partial=False, round_timeout=2.0)
    result = runtime.run(rounds=1)
    assert result.rounds[0].outcome == "timeout"
    assert result.generation == 0
    np.testing.assert_allclose(
        result.final_params[WEIGHT_PARAM],
        initial_params(cfg.in_features)[WEIGHT_PARAM],
    )
    undecided = [
        e for e in result.diagnostics_events if e["reason"] == "missing_gradient"
    ]
    assert undecided


def test_missing_gradient_partial_mode_matches_independent_per_slot_expectation():
    cfg = RuntimeConfig()
    shards = make_shards(cfg.in_features, SIZES, seed=42)
    specs = _specs(shards, w1={"missing_params": (BIAS_PARAM,)})
    runtime = _build_runtime(specs, cfg, allow_partial=True)
    result = runtime.run(rounds=1)
    assert result.rounds[0].outcome == "committed"

    # Independently re-derive per-slot means purely from reference formulas:
    # per-worker gradient sums == joint mean over that shard times its n.
    base = initial_params(cfg.in_features)
    sums = []
    for x, y in shards:
        mean = joint_linear_mse(base, x, y)
        n = x.shape[0]
        sums.append(({k: v * n for k, v in mean.items()}, n))
    w_mean = reference_weighted_mean(sums, [WEIGHT_PARAM])
    # bias has no contribution from w1 (3-sample shard)
    b_mean = reference_weighted_mean([sums[0], sums[2]], [BIAS_PARAM])
    expected = reference_sgd_step(
        base,
        {WEIGHT_PARAM: w_mean[WEIGHT_PARAM], BIAS_PARAM: b_mean[BIAS_PARAM]},
        cfg.lr,
    )
    np.testing.assert_allclose(result.final_params[WEIGHT_PARAM], expected[WEIGHT_PARAM], atol=TOL)
    np.testing.assert_allclose(result.final_params[BIAS_PARAM], expected[BIAS_PARAM], atol=TOL)


def test_corrupt_base_token_is_rejected_in_worker_reports():
    cfg = RuntimeConfig()
    shards = make_shards(cfg.in_features, SIZES, seed=42)
    specs = _specs(shards, w0={"corrupt_base_token": True})
    runtime = _build_runtime(specs, cfg, round_timeout=2.0)
    result = runtime.run(rounds=1)
    # w0's every bucket is rejected -> barrier never completes -> timeout,
    # and the failure category is recorded on both sides.
    assert result.generation == 0
    w0_summary = result.worker_reports["w0"][0]
    assert w0_summary["rejected"]
    assert {r["reason"] for r in w0_summary["rejected"]} == {"wrong_base_generation"}
    server_side = {
        e["reason"] for e in result.diagnostics_events if e["worker_id"] == "w0"
    }
    assert "wrong_base_generation" in server_side
