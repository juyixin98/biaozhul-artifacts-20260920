#!/usr/bin/env python3
"""End-to-end local verification over synthetic fixtures.

Runs the four mandated scenarios plus aliasing/training, and prints:

* numeric verdict against the independent NumPy oracle,
* reuse vs no-reuse accounting (arena bytes, per-wave peak, cumulative allocs),
* conflict checks and the planner's reasons,
* the failure-category distinctions,
* locations of replayable JSONL logs.

Exit code is non-zero if any check fails, so it doubles as a smoke gate.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tensor_mem import fixtures as fx  # noqa: E402
from tensor_mem.errors import (  # noqa: E402
    ComputationError,
    InputValidationError,
    ResourceExhaustedError,
    StateConflictError,
)
from tensor_mem.executor import Executor  # noqa: E402
from tensor_mem.liveness import analyze_liveness  # noqa: E402
from tensor_mem.planner import plan_graph  # noqa: E402
from tensor_mem.runlog import RunLogger  # noqa: E402
from tensor_mem.state import TrainingState  # noqa: E402

LOG_DIR = ROOT / "logs"
ALIGN = 64

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" -- {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def numeric_and_memory(name: str, case, budget=None, external=frozenset()) -> None:
    log = RunLogger(run_id=f"demo-{name}", path=LOG_DIR / f"demo-{name}.jsonl")
    ex = Executor(case.graph(), budget=budget, logger=log, external_names=external)
    naive = plan_graph(
        case.graph(), alignment=ALIGN, allow_reuse=False, external_names=external
    )
    handles, report = ex.execute(case.feeds, run_id=f"demo-{name}")
    ref = fx.reference_outputs(case)
    numeric_ok = True
    max_err = 0.0
    for out, h in handles.items():
        err = float(np.max(np.abs(h.array.astype(np.float64) - ref[out].astype(np.float64))))
        max_err = max(max_err, err)
        numeric_ok &= np.allclose(h.array, ref[out], rtol=1e-5, atol=1e-5)
    check(f"{name}: outputs match independent oracle", numeric_ok,
          f"max abs err={max_err:.3e}")
    check(f"{name}: planner reported zero conflicts",
          ex.plan.conflicts == [], f"{len(ex.plan.conflicts)} conflicts")
    check(f"{name}: arena never exceeds no-reuse reservation "
          "(strictly smaller when disjoint intervals exist)",
          ex.plan.total_pool_bytes <= naive.total_pool_bytes,
          f"{ex.plan.total_pool_bytes}B vs no-reuse {naive.total_pool_bytes}B")
    check(f"{name}: cumulative acquisitions match plan",
          report.total_acquired_bytes
          == sum(s.capacity for s in ex.plan.slots.values()) + 0,
          f"acquired={report.total_acquired_bytes}B")
    print(f"         waves (resident bytes): "
          f"{[w['resident_bytes'] for w in report.wave_resident]}")
    print(f"         peak_resident={report.plan_peak_resident}B "
          f"persistent={ex.plan.persistent_bytes}B "
          f"slots={len(ex.plan.slots)} records={len(ex.plan.records)}")
    for h in handles.values():
        h.release()
    log.close()


def main() -> int:
    LOG_DIR.mkdir(exist_ok=True)
    # Demo run ids are fixed for readability; start each demo from a clean log.
    for old in LOG_DIR.glob("demo-*.jsonl"):
        old.unlink()

    section("1. Diamond graph: dead temp reused for join output")
    numeric_and_memory("diamond", fx.diamond_case())
    g = fx.build_diamond_graph()
    live = analyze_liveness(g, ALIGN)
    conflicts = {
        tuple(sorted((c.record_a, c.record_b))): c.reason
        for c in live.all_pairwise_conflicts(g)
    }
    check("parallel branches flagged parallel_branch",
          conflicts[(("val::a", "val::m"))] == "parallel_branch")
    plan = plan_graph(g, alignment=ALIGN)
    check("join output reuses dead t slot",
          plan.slot_of("val::t") == plan.slot_of("val::y"))
    check("diamond exact peak = 256B", plan.peak_resident == 256)

    section("2. Long-lived output retained until client release")
    numeric_and_memory("longlived", fx.long_lived_case())
    ex = Executor(fx.build_long_lived_graph())
    handles, _ = ex.execute(fx.long_lived_case().feeds, run_id="demo-retain")
    retained_slot = ex.plan.slot_of("val::early")
    check("retained early output has a pinned slot", bool(retained_slot))
    check("retained_bytes charged while held", ex.retained_bytes() == 128)
    try:
        handles["early"].release()
        handles["early"].numpy()
        check("use after release rejected", False)
    except StateConflictError:
        check("use after release rejected", True)
    handles["final"].release()
    check("retained_bytes zero after all releases", ex.retained_bytes() == 0)

    section("3. Shape jump: capacity deficit forces replan, never OOB reuse")
    static = fx.build_dynamic_matmul_graph()
    log = RunLogger(run_id="demo-dynamic", path=LOG_DIR / "demo-dynamic.jsonl")
    ex = Executor(static, logger=log)
    check("static plan peak = 256B", ex.plan.peak_resident == 256)
    big = fx.dynamic_case(m=32, k=4, n=16)
    handles, report = ex.execute(big.feeds, run_id="demo-shape-jump")
    check("oversize run triggered replan", report.replanned,
          f"{len(report.capacity_deficits)} deficits")
    check("every deficit is required > capacity",
          all(d["required"] > d["capacity"] for d in report.capacity_deficits))
    check("grown outputs numerically correct",
          np.allclose(handles["y"].array, fx.reference_outputs(big)["y"], rtol=1e-5))
    # wave 1 holds c (32x16=2048B) and retained y (2048B) -> 4096B peak
    check("replanned peak grew to 4096B", report.plan_peak_resident == 4096)
    handles["y"].release()
    same = fx.dynamic_case(m=32, k=4, n=16, seed=123)
    _, report2 = ex.execute(same.feeds, run_id="demo-shape-same")
    check("same-size rerun does not replan", report2.replanned is False)
    small = fx.dynamic_case(m=2, k=4, n=2)
    h3, report3 = ex.execute(small.feeds, run_id="demo-shape-shrink")
    check("shrink run reuses larger bindings, no replan",
          report3.replanned is False
          and np.allclose(h3["y"].array, fx.reference_outputs(small)["y"], rtol=1e-5))
    h3["y"].release()
    log.close()
    try:
        ex.execute({"a": np.zeros((2, 4, 1), np.float32),
                    "b": np.zeros((4, 2), np.float32)}, run_id="demo-badrank")
        check("rank change rejected", False)
    except InputValidationError as e:
        check("rank change rejected as input_error",
              e.category == "input_error")

    section("4. Concurrent branches: disjoint buffers, threaded execution")
    case = fx.concurrent_case(4)
    numeric_and_memory("concurrent", case)
    plan = plan_graph(case.graph())
    branch_slots = {plan.slot_of(f"val::o{i}") for i in range(4)}
    ws_slots = {plan.slot_of(f"ws::n_branch_{i}") for i in range(4)}
    check("4 branch outputs occupy 4 distinct slots", len(branch_slots) == 4)
    check("4 workspaces occupy 4 distinct slots", len(ws_slots) == 4)
    check("output and workspace slot sets disjoint",
          branch_slots.isdisjoint(ws_slots))
    # 5 feeds (x + 4 weights) + 4 outputs + 4 workspaces, all in one wave
    check("concurrent peak = 13 records * 64B = 832B",
          plan.peak_resident == 832)

    section("5. Aliasing: reshape/transpose views share source storage")
    numeric_and_memory("alias", fx.alias_case())
    ex = Executor(fx.build_alias_graph())
    handles, _ = ex.execute(fx.alias_case().feeds, run_id="demo-alias")
    check("transpose view shares memory with source",
          np.shares_memory(handles["r"].array, handles["z"].array))
    for h in handles.values():
        h.release()

    section("6. Training state: external parameter feed + SGD step")
    graph = fx.build_linear_grad_graph()
    state = TrainingState("sgd")
    state.declare_parameter("W", "float32", (3, 4))
    state.prepare(seed=2026)
    w0 = state.snapshot()["W"].copy()
    tlog = RunLogger(run_id="demo-train", path=LOG_DIR / "demo-train.jsonl")
    tex = Executor(graph, external_names=frozenset({"W"}), logger=tlog)
    case = fx.linear_grad_case(seed=2026)
    feeds = dict(case.feeds)
    feeds["W"] = state.parameter_array("W")
    state.begin_step()
    handles, _ = tex.execute(feeds, run_id="demo-train-step")
    grad = handles["grad_W"].array
    state.apply_gradients({"W": grad}, lr=0.05)
    check("SGD update matches independent formula",
          np.allclose(state.parameter_array("W"), w0 - 0.05 * grad, rtol=1e-6))
    check("parameter storage external, not pool-placed",
          "val::W" not in tex.plan.assignment
          and tex.plan.records["val::W"].external)
    check("external bytes counted at wave 0",
          tex.plan.wave_stats[0].external_bytes == 64)
    for h in handles.values():
        h.release()
    tlog.close()

    section("7. Failure categories are distinguishable")
    expected = [
        ("input_error", lambda: Executor(fx.build_diamond_graph()).execute({})),
        ("computation_failure",
         lambda: Executor(fx.build_diamond_graph()).execute(
             {"x": np.full((4, 4), np.nan, np.float32)})),
    ]
    for cat, action in expected:
        try:
            action()
            check(f"{cat} raised", False)
        except Exception as e:  # noqa: BLE001
            check(f"{cat} raised with stable category", e.category == cat)

    try:
        plan_graph(fx.build_concurrent_branches_graph(3), budget=100)
        check("resource_exhausted raised", False)
    except ResourceExhaustedError as e:
        check("resource_exhausted with exact overage",
              e.category == "resource_exhausted" and e.details["over_bytes"] == 540)

    s = TrainingState()
    s.declare_parameter("W", "float32", (2, 2))
    s.prepare()
    s.begin_step()
    try:
        s.begin_step()
        check("state_conflict raised", False)
    except StateConflictError as e:
        check("state_conflict on double begin_step", e.category == "state_conflict")

    section("Result")
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    print(f"\nReplayable logs written under: {LOG_DIR}")
    print("Inspect with: python3 scripts/replay_log.py logs/<run>.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
