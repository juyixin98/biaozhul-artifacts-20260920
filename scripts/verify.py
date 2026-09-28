#!/usr/bin/env python3
"""Local verification driver.

Runs the full scenario matrix through the Engine, prints a human-readable
report and writes structured records to logs/runs.jsonl. Every scenario gets a
deterministic run id ``verify-<case>[-<suffix>]`` so a failure can be replayed
with ``--replay <run_id>``.

Exit code is 0 only when every scenario reaches its *expected* verdict; a
scenario that expects a specific failure category fails the run if it does not.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tenmem import fixtures as fx  # noqa: E402
from tenmem.errors import TenmemError  # noqa: E402
from tenmem.runlog import RunLogger  # noqa: E402
from tenmem.service import Engine  # noqa: E402
from tenmem.verification import assert_plan_invariants  # noqa: E402


def _expect_ok(engine, run_id, graph, feeds, ref_outputs, parallel=False):
    outcome = engine.run(graph, feeds, run_id=run_id, parallel=parallel)
    assert_plan_invariants(outcome.plan)
    for name, expected in ref_outputs.items():
        got = outcome.outputs[name]
        if not np.allclose(got, expected, rtol=1e-5, atol=1e-5):
            raise AssertionError(f"{run_id}: output {name!r} disagrees with independent reference")
    return outcome


def scenario_matrix(engine: Engine) -> list[dict]:
    rows: list[dict] = []

    # 1. Diamond: reuse vs no-reuse vs hand-written reference.
    g, feeds = fx.build_diamond(8), fx.diamond_feeds(8)
    o = _expect_ok(engine, "verify-diamond", g, feeds, {"y": fx.diamond_reference(feeds["x"])})
    rows.append(dict(run_id="verify-diamond", case="diamond", expected="ok",
                     status="ok", peak=o.plan.peak_bytes, no_reuse=o.no_reuse_peak_bytes,
                     equivalent=o.numerically_equivalent, replanned=False))

    # 2. Long-lived outputs pinned until client release.
    g, feeds = fx.build_long_lived(16, 4), fx.long_lived_feeds(16)
    early, final = fx.long_lived_reference(feeds["x"])
    o = _expect_ok(engine, "verify-long-lived", g, feeds, {"early": early, "final": final})
    pinned_before = engine.session("verify-long-lived").pinned_bytes()
    engine.release("verify-long-lived", "early")
    engine.release("verify-long-lived", "final")
    assert engine.session("verify-long-lived").pinned_bytes() == 0
    rows.append(dict(run_id="verify-long-lived", case="long_lived", expected="ok",
                     status="ok", peak=o.plan.peak_bytes, no_reuse=o.no_reuse_peak_bytes,
                     pinned_before_release=pinned_before, equivalent=True))

    # 3a. Shape within bound — no replan.
    g = fx.build_shape_mutate(capacity=64, bound_n=32)
    feeds = fx.shape_mutate_feeds(10, 64)
    o = _expect_ok(engine, "verify-shape-inbound", g, feeds,
                   {"act": fx.shape_mutate_reference(feeds["data"], 10)})
    rows.append(dict(run_id="verify-shape-inbound", case="shape_mutate", expected="ok",
                     status="ok", peak=o.plan.peak_bytes, replanned=o.replanned))

    # 3b. Shape jump past bound — automatic replan, then correct result.
    feeds = fx.shape_mutate_feeds(48, 64)
    o = _expect_ok(engine, "verify-shape-jump", g, feeds,
                   {"act": fx.shape_mutate_reference(feeds["data"], 48)})
    assert o.replanned and o.replan_reason["category"] == "replanning_required"
    rows.append(dict(run_id="verify-shape-jump", case="shape_mutate",
                     expected="replanning_required->ok", status="ok",
                     peak=o.plan.peak_bytes, replanned=True))

    # 4. Concurrent branches, threaded execution.
    g, feeds = fx.build_parallel_branches(12, 3), fx.parallel_feeds(12)
    o = _expect_ok(engine, "verify-parallel", g, feeds,
                   {"m2": fx.parallel_reference(feeds["x"])}, parallel=True)
    rows.append(dict(run_id="verify-parallel", case="parallel_branches", expected="ok",
                     status="ok", peak=o.plan.peak_bytes, no_reuse=o.no_reuse_peak_bytes,
                     parallel=True, equivalent=True))

    # 5. Alias union shares storage (covered by invariants + reference).
    g, feeds = fx.build_alias(8), fx.alias_feeds(8)
    o = _expect_ok(engine, "verify-alias", g, feeds, {"c": fx.alias_reference(feeds["x"])})
    rows.append(dict(run_id="verify-alias", case="alias_chain", expected="ok",
                     status="ok", peak=o.plan.peak_bytes, equivalent=True))

    # 6. Workspace pressure counted in peak.
    g, feeds = fx.build_workspace_matmul(), fx.workspace_feeds()
    o = _expect_ok(engine, "verify-workspace", g, feeds,
                   {"sm": fx.workspace_reference(feeds["a"], feeds["b"])})
    rows.append(dict(run_id="verify-workspace", case="workspace_matmul", expected="ok",
                     status="ok", peak=o.plan.peak_bytes, no_reuse=o.no_reuse_peak_bytes))

    # 7. Negative cases: each must surface its precise category.
    rows.extend(_negative_scenarios(engine))
    return rows


def _negative_scenarios(engine: Engine) -> list[dict]:
    out: list[dict] = []

    def expect_failure(run_id, case, category, fn):
        try:
            fn()
        except TenmemError as exc:
            ok = exc.category == category
            out.append(dict(run_id=run_id, case=case, expected=category,
                            status="ok" if ok else "mismatch",
                            observed=exc.category, message=exc.message))
            if not ok:
                raise AssertionError(f"{run_id}: expected {category}, got {exc.category}")
        else:
            raise AssertionError(f"{run_id}: expected {category} but no error was raised")

    expect_failure("verify-bad-input", "missing_feed", "input_error",
                   lambda: engine.run(fx.build_diamond(8), {}))

    def too_small_budget():
        g = fx.build_diamond(8)
        engine.make_plan(g, max_bytes=1)
    expect_failure("verify-budget", "budget", "resource_exhausted", too_small_budget)

    def state_conflict():
        from tenmem.executor import Session
        from tenmem.planner.memory import plan_memory

        g = fx.build_diamond(8)
        s = Session(g, plan_memory(g)).run(fx.diamond_feeds(8))
        s.run(fx.diamond_feeds(8))  # pinned output still held
    expect_failure("verify-state", "rerun_while_pinned", "state_conflict", state_conflict)

    def compute_failure():
        # Length beyond source capacity: replan succeeds, kernel must fail.
        from tenmem.service import infer_concrete_graph
        from tenmem.executor import execute
        from tenmem.planner.memory import plan_memory

        g = fx.build_shape_mutate(capacity=64, bound_n=32)
        feeds = fx.shape_mutate_feeds(70, 64)
        concrete = infer_concrete_graph(g, feeds)
        execute(concrete, plan_memory(concrete), feeds)
    expect_failure("verify-compute", "tile_beyond_source", "computation_failed", compute_failure)

    return out


def print_table(rows: list[dict]) -> None:
    print(f"{'run_id':28s} {'case':20s} {'expected':24s} {'status':8s} {'peak':>7s} {'no_reuse':>8s}")
    print("-" * 100)
    for r in rows:
        print(f"{r['run_id']:28s} {r['case']:20s} {r['expected']:24s} {r['status']:8s} "
              f"{r.get('peak', '-'):>7} {r.get('no_reuse', '-'):>8}")


def main() -> int:
    parser = argparse.ArgumentParser(description="tenmem local verification")
    parser.add_argument("--log-dir", default=str(ROOT / "logs"))
    parser.add_argument("--replay", metavar="RUN_ID", help="print JSONL records of a run and exit")
    args = parser.parse_args()

    logger = RunLogger(args.log_dir)
    if args.replay:
        records = logger.replay(args.replay)
        if not records:
            print(f"no records for run {args.replay!r}", file=sys.stderr)
            return 2
        for rec in records:
            print(json.dumps(rec, sort_keys=True))
        return 0

    engine = Engine(logger)
    rows = scenario_matrix(engine)
    print_table(rows)
    ok = all(r["status"] == "ok" for r in rows)
    print()
    print(f"scenarios: {len(rows)}  passed: {sum(r['status'] == 'ok' for r in rows)}  "
          f"failed: {sum(r['status'] != 'ok' for r in rows)}")
    print(f"log: {Path(args.log_dir) / 'runs.jsonl'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
