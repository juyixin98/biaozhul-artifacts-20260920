#!/usr/bin/env python3
"""Acceptance walk-through for the workload drain planner.

Runs four scenarios entirely against the local simulator (no network) and
prints, for every executed step, the step type and its evaluated
preconditions. After each transition the PDB safety audit is checked. The
script exits non-zero if any invariant is violated.

Scenarios:
  1. happy-path single-node drain
  2. two-node simultaneous drain under three intersecting PDBs
  3. replacement pod that never becomes ready (drain must WAIT safely)
  4. cancellation mid-drain

Run:  python3 scripts/demo.py
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.executor import (  # noqa: E402
    DrainState, ExecutorError, advance, create_drain, request_cancel,
)
from app.eviction import audit_snapshot  # noqa: E402
from app.models import Snapshot  # noqa: E402
from app.security import new_secret  # noqa: E402
from app.simulator import Simulator  # noqa: E402

EX = ROOT / "examples"


def load(name: str) -> Snapshot:
    data = json.loads((EX / name).read_text())
    return Snapshot.model_validate(data)


def assert_audit_clean(sim: Simulator, label: str) -> None:
    violations = audit_snapshot(sim.snap)
    if violations:
        for v in violations:
            print(f"    !! VIOLATION {v.pdb}: {v.detail}")
        raise SystemExit(f"FAIL: safety violation during {label}")


def step_drain(sim, drain, token, *, max_ticks=40, label="drain"):
    budget = max_ticks
    executed = 0
    while True:
        result = advance(drain, token, tick_wait=budget)
        token = result.token
        budget -= result.ticks_used
        kind = result.current_step["kind"] if result.current_step else "-"
        print(f"    -> executed={result.executed} state={result.state} "
              f"next={kind} ticks_used={result.ticks_used} :: {result.message}")
        for p in result.preconditions:
            mark = "ok " if p.satisfied else "XX "
            print(f"         [{mark}{p.severity}] {p.id}: {p.detail}")
        assert_audit_clean(sim, label)
        if result.executed:
            executed += 1
        if result.state in ("COMPLETE", "BLOCKED", "CANCELLED"):
            break
        if not result.executed:
            break
    return token, drain.state, executed


def scenario_basic() -> None:
    print("\n=== Scenario 1: single-node drain (minAvailable=2 of 3) ===")
    sim = Simulator(load("snapshot-basic.json"))
    drain = create_drain(sim, "d1", new_secret(), nodes=["node-a"])
    print("  predicted steps:")
    for s in drain.plan.steps:
        print(f"    - {s.kind} {getattr(s, 'pods', getattr(s, 'node', ''))}")
    step_drain(sim, drain, drain.token(), label="basic")
    assert drain.state == DrainState.COMPLETE
    assert all(p.node != "node-a" for p in sim.snap.pods)
    print("  RESULT: node-a empty; audit clean")


def scenario_two_nodes() -> None:
    print("\n=== Scenario 2: two-node drain, 3 intersecting PDBs ===")
    sim = Simulator(load("snapshot-multi-pdb.json"))
    drain = create_drain(sim, "d2", new_secret(), nodes=["node-1", "node-2"])
    waves = [s for s in drain.plan.steps if s.kind == "EvictWave"]
    print(f"  predicted {len(waves)} wave(s); sizes "
          f"{[len(w.pods) for w in waves]} (shared PDB maxUnavailable=1)")
    _, state, _ = step_drain(sim, drain, drain.token(), max_ticks=40,
                             label="two-node")
    assert state == DrainState.COMPLETE
    ready = [p for p in sim.snap.pods if p.ready and p.phase == "Running"]
    assert len(ready) == 8, f"expected 8 ready pods, got {len(ready)}"
    print("  RESULT: node-1/node-2 empty; 8 ready pods; audit clean")


def scenario_stuck_replacement() -> None:
    print("\n=== Scenario 3: replacement pod never becomes ready ===")
    snap = load("snapshot-basic.json")
    for dep in snap.deployments:
        dep.ready_delay = 10 ** 6  # effectively never
    sim = Simulator(snap)
    drain = create_drain(sim, "d3", new_secret(), nodes=["node-a"])
    token = drain.token()
    # cordon
    r = advance(drain, token, 0)
    token = r.token
    print(f"    cordon: {r.message}")
    # first wave evicts, replacement hangs
    r = advance(drain, token, 0)
    token = r.token
    print(f"    wave: {r.message}")
    assert_audit_clean(sim, "stuck-replacement")
    # try to make progress within a small tick budget -> must WAIT, not COMPLETE
    r = advance(drain, token, tick_wait=3)
    print(f"    after 3 ticks: executed={r.executed} state={r.state}")
    bad = [p.id for p in r.preconditions if not p.satisfied]
    print(f"    unsatisfied preconditions: {bad}")
    assert r.executed is False and r.state == DrainState.WAITING.value
    assert_audit_clean(sim, "stuck-replacement")
    print("  RESULT: drain safely WAITING, no constraint broken, no false completion")


def scenario_cancel() -> None:
    print("\n=== Scenario 4: cancellation mid-drain ===")
    snap = load("snapshot-basic.json")
    for dep in snap.deployments:
        dep.ready_delay = 3
    sim = Simulator(snap)
    drain = create_drain(sim, "d4", new_secret(), nodes=["node-a"])
    token = drain.token()
    token = advance(drain, token, 0).token   # cordon
    token = advance(drain, token, 0).token   # wave 0 (replacement maturing)
    request_cancel(drain, token)
    print("    requested cancel")
    try:
        advance(drain, drain.token(), tick_wait=5)
        raise SystemExit("FAIL: advance after cancel was accepted")
    except ExecutorError as exc:
        assert exc.code == "cancelled"
        print(f"    further advance refused: {exc.code}")
    assert not sim.node("node-a").schedulable, "node must stay cordoned"
    # Let the in-flight replacement mature; the workload heals with no more
    # evictions and no violation.
    for _ in range(6):
        sim.tick(1)
    assert_audit_clean(sim, "cancel")
    ready = [p for p in sim.snap.pods if p.ready and p.phase == "Running"]
    assert len(ready) == 3
    print("  RESULT: cancelled; node cordoned; workload healed to 3 ready; audit clean")


def main() -> int:
    scenario_basic()
    scenario_two_nodes()
    scenario_stuck_replacement()
    scenario_cancel()
    print("\nALL ACCEPTANCE SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
