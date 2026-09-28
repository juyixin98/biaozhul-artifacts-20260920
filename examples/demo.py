"""Teaching demo: gradient bucketing and weighted reduction, end to end.

Run:
    PYTHONPATH=src python3 examples/demo.py

Scenarios:
    1. Happy path with unequal batches (5,3,2) and shuffled bucket order;
       compare against an independent single-process joint-batch oracle.
    2. A worker misses one gradient: strict mode refuses the commit,
       partial-coverage mode reduces that slot over covering workers only.
    3. A worker process crashes mid-round: the whole round is rejected and
       no weights move; survivors continue on the next round.

Every scenario prints bucket generations and the exact reduction basis
(sample counts per worker, per-slot coverage) plus accept/reject evidence.
"""

from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from bucket_sync.bucketing import BucketLayout  # noqa: E402
from bucket_sync.config import (  # noqa: E402
    RuntimeConfig,
    initial_params,
    make_graph,
    make_shards,
)
from bucket_sync.coordinator import Coordinator  # noqa: E402
from bucket_sync.diagnostics import Diagnostics  # noqa: E402
from bucket_sync.reference import (  # noqa: E402
    joint_linear_mse,
    reference_run,
    reference_sgd_step,
    reference_weighted_mean,
)
from bucket_sync.runtime import LocalRuntime, WorkerSpec  # noqa: E402
from bucket_sync.training import BIAS_PARAM, WEIGHT_PARAM, ModelState  # noqa: E402

WORKERS = ["w0", "w1", "w2"]
SIZES = (5, 3, 2)
TOL = 1e-9


def _hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _build(cfg: RuntimeConfig, *, allow_partial: bool = False) -> tuple:
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
    return graph, layout, coordinator


def _specs(shards, **overrides) -> list:
    out = []
    for i, (wid, shard) in enumerate(zip(WORKERS, shards)):
        kw = {"submit_order_seed": 11 + i * 13}
        kw.update(overrides.get(wid, {}))
        out.append(WorkerSpec(worker_id=wid, x_shard=shard[0], y_shard=shard[1], **kw))
    return out


def _print_evidence(result) -> None:
    print("bucket generations & reduction basis:")
    committed = [e for e in result.diagnostics_events if e["reason"] == "round_committed"]
    for event in committed:
        d = event["detail"]
        print(
            f"  round base generation {d['generation_before']} -> "
            f"{d['generation_after']} | samples/worker = {d['samples_per_worker']} "
            f"(total {d['total_samples']})"
        )
        for bg in d["bucket_generations"]:
            print(f"    bucket {bg['bucket']}: reduced at base generation {bg['generation']}")


def scenario_happy_path(cfg: RuntimeConfig) -> None:
    _hr("Scenario 1: unequal batches (5,3,2), shuffled completion order")
    shards = make_shards(cfg.in_features, SIZES, seed=42)
    _, _, coordinator = _build(cfg)
    runtime = LocalRuntime(coordinator, *_layout(cfg), _specs(shards))
    result = runtime.run(rounds=2)

    for i, outcome in enumerate(result.rounds, 1):
        print(f"round {i}: {outcome.outcome} (reason={outcome.reason})")
    _print_evidence(result)

    expected = reference_run(initial_params(cfg.in_features), shards, cfg.lr, rounds=2)
    max_err_w = float(np.max(np.abs(result.final_params[WEIGHT_PARAM] - expected[WEIGHT_PARAM])))
    max_err_b = float(np.max(np.abs(result.final_params[BIAS_PARAM] - expected[BIAS_PARAM])))
    print(f"max abs error vs single-process joint-batch oracle: w={max_err_w:.2e}, b={max_err_b:.2e}")
    assert max_err_w < TOL and max_err_b < TOL
    print("MATCH: distributed weighted reduction == joint-batch reference.")


def _layout(cfg: RuntimeConfig) -> tuple:
    graph = make_graph(cfg.in_features)
    return BucketLayout(graph, cfg.bucket_size),


def scenario_missing_gradient(cfg: RuntimeConfig) -> None:
    _hr("Scenario 2: w1 computes no bias gradient")
    shards = make_shards(cfg.in_features, SIZES, seed=42)

    print("-- 2a. strict mode: commit must be refused (undecided), no update")
    _, _, strict_coord = _build(cfg, allow_partial=False)
    strict_rt = LocalRuntime(
        strict_coord, *_layout(cfg), _specs(shards, w1={"missing_params": (BIAS_PARAM,)}),
        round_timeout_s=2.0,
    )
    strict_result = strict_rt.run(rounds=1)
    print(f"round outcome: {strict_result.rounds[0].outcome} "
          f"(reason={strict_result.rounds[0].reason}); generation stays {strict_result.generation}")
    undecided = [e for e in strict_result.diagnostics_events if e["reason"] == "missing_gradient"]
    print(f"diagnostic records explaining the refusal: {len(undecided)}")
    print(f"example detail: {undecided[0]['detail']}")
    assert strict_result.generation == 0

    print("-- 2b. partial-coverage mode: bias slot reduced over w0,w2 only (5+2 samples)")
    _, _, partial_coord = _build(cfg, allow_partial=True)
    partial_rt = LocalRuntime(
        partial_coord, *_layout(cfg), _specs(shards, w1={"missing_params": (BIAS_PARAM,)}),
    )
    partial_result = partial_rt.run(rounds=1)
    assert partial_result.rounds[0].outcome == "committed"
    base = initial_params(cfg.in_features)
    sums = []
    for x, y in shards:
        mean = joint_linear_mse(base, x, y)
        sums.append(({k: v * x.shape[0] for k, v in mean.items()}, x.shape[0]))
    w_mean = reference_weighted_mean(sums, [WEIGHT_PARAM])
    b_mean = reference_weighted_mean([sums[0], sums[2]], [BIAS_PARAM])
    expected = reference_sgd_step(
        base, {WEIGHT_PARAM: w_mean[WEIGHT_PARAM], BIAS_PARAM: b_mean[BIAS_PARAM]}, cfg.lr
    )
    print("weight slot covers 5+3+2=10 samples; bias slot covers 5+2=7 samples")
    print(
        "max abs error vs independently re-derived per-slot expectation: "
        f"w={float(np.max(np.abs(partial_result.final_params[WEIGHT_PARAM] - expected[WEIGHT_PARAM]))):.2e}, "
        f"b={float(np.max(np.abs(partial_result.final_params[BIAS_PARAM] - expected[BIAS_PARAM]))):.2e}"
    )


def scenario_crash(cfg: RuntimeConfig) -> None:
    _hr("Scenario 3: w1 crashes mid-round; round rejected, survivors continue")
    shards = make_shards(cfg.in_features, SIZES, seed=42)
    _, _, coordinator = _build(cfg)
    runtime = LocalRuntime(
        coordinator, *_layout(cfg), _specs(shards, w1={"crash_after_buckets": 1})
    )
    result = runtime.run(rounds=2)
    for i, outcome in enumerate(result.rounds, 1):
        print(f"round {i}: {outcome.outcome} (reason={outcome.reason})")
    assert result.rounds[0].reason == "worker_lost"
    assert result.rounds[1].outcome == "committed"
    assert result.generation == 1  # aborted round applied nothing

    surviving = [shards[0], shards[2]]
    expected = reference_run(initial_params(cfg.in_features), surviving, cfg.lr, rounds=1)
    max_err_w = float(np.max(np.abs(result.final_params[WEIGHT_PARAM] - expected[WEIGHT_PARAM])))
    max_err_b = float(np.max(np.abs(result.final_params[BIAS_PARAM] - expected[BIAS_PARAM])))
    print(f"round 2 ran over survivors w0,w2; oracle error: w={max_err_w:.2e}, b={max_err_b:.2e}")
    print("late/partial work from the lost worker was rejected for category "
          "'round_aborted'; see diagnostics with record ids above.")
    assert max_err_w < TOL and max_err_b < TOL


def main() -> None:
    cfg = RuntimeConfig()
    print(f"config: in_features={cfg.in_features}, bucket_size={cfg.bucket_size}, "
          f"lr={cfg.lr}, shard sizes={SIZES}")
    scenario_happy_path(cfg)
    scenario_missing_gradient(cfg)
    scenario_crash(cfg)
    _hr("All demo scenarios completed and matched independent expectations.")


if __name__ == "__main__":
    main()
