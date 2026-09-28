#!/usr/bin/env python3
"""Standalone acceptance/verification harness.

Runs the four required validation dimensions on bundled synthetic fixtures and
judges every returned plan with the *independent* oracle (tests/oracle.py),
which parses the raw YAML itself and re-simulates facts/resources without using
the planner under test:

  1. recursive decomposition  (assembly depth-5, logistics via-hub)
  2. method mutual exclusion  (direct vs via-hub selected purely by state)
  3. shared resource          (capacity-1 dock across partial-order siblings)
  4. partial-order conflict   (topological orders exist, none executable)

It also asserts concrete terminal failure categories for every infeasible
fixture.  Checks that cannot be executed (e.g. a fixture failed to load) are
listed separately under "NOT EXECUTED" and are never reported as passed.

Exit code: 0 only if every executed check passed and none were skipped-fatal.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from htn_planner import Planner, load_domain, load_problem  # noqa: E402
from tests.oracle import IndependentOracle, OracleFailure  # noqa: E402

PROBLEM_DIR = REPO_ROOT / "fixtures" / "problems"
DOMAIN_DIR = REPO_ROOT / "fixtures" / "domains"


@dataclass
class Check:
    dimension: str
    name: str
    passed: bool
    detail: str


def _raw(problem_file: str) -> dict:
    with open(PROBLEM_DIR / problem_file, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _plan(problem_file: str):
    problem = load_problem(PROBLEM_DIR / problem_file)
    domain = load_domain(DOMAIN_DIR / f"{problem.domain}.yaml")
    return Planner(domain).plan(problem), _raw(problem_file)


def _oracle(raw: dict) -> IndependentOracle:
    return IndependentOracle(
        DOMAIN_DIR / f"{raw['domain']}.yaml",
        raw.get("initial_facts", []),
        raw.get("initial_resources"),
    )


def _grounded(result) -> list[tuple[str, tuple[str, ...]]]:
    return [
        (result.nodes[n].primitive, tuple(result.nodes[n].args))
        for n in result.execution_order
    ]


def main() -> int:
    checks: list[Check] = []
    not_executed: list[str] = []

    # 1. Recursive decomposition: concrete 5-leaf assembly tree -------------
    try:
        res, raw = _plan("assembly_deep.yaml")
        seq = _grounded(res)
        expected = [
            ("take_part", ("leaf",)),
            ("join", ("leaf", "a3")),
            ("join", ("a3", "a2")),
            ("join", ("a2", "a1")),
            ("join", ("a1", "root")),
        ]
        ok = res.feasible and seq == expected
        oracle = _oracle(raw)
        oracle.replay(res.model_dump()["nodes"], res.execution_order)
        ok = ok and ("ready", "root") in oracle.facts
        checks.append(Check(
            "recursion", "assembly depth-5 expands to exact leaf chain",
            ok, f"leaves={seq}",
        ))
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        not_executed.append(f"recursion/assembly: {exc}")

    # Recursive routing through the hub -------------------------------------
    try:
        res, raw = _plan("logistics_via_hub.yaml")
        moves = [a for a in _grounded(res) if a[0] == "move"]
        oracle = _oracle(raw)
        oracle.replay(res.model_dump()["nodes"], res.execution_order)
        ok = (
            res.feasible
            and moves == [
                ("move", ("truck", "depot", "hub")),
                ("move", ("truck", "hub", "city")),
            ]
            and ("at", "box", "city") in oracle.facts
        )
        checks.append(Check(
            "recursion", "via-hub shipment recurses to a concrete two-leg route",
            ok, f"moves={moves}",
        ))
    except Exception as exc:  # noqa: BLE001
        not_executed.append(f"recursion/via-hub: {exc}")

    # 2. Method mutual exclusion: state flips the chosen method -------------
    try:
        direct, _ = _plan("logistics_direct.yaml")
        via, _ = _plan("logistics_via_hub.yaml")
        d = next(n for n in direct.nodes.values() if n.task == "haul")
        v = next(n for n in via.nodes.values() if n.task == "haul")
        ok = d.method == "m-haul-direct" and v.method == "m-haul-via-hub"
        checks.append(Check(
            "mutex", "connected state selects direct; disconnected selects hub",
            ok, f"direct={d.method}, via={v.method}",
        ))
    except Exception as exc:  # noqa: BLE001
        not_executed.append(f"mutex: {exc}")

    # 3. Shared resource: partial-order feasible, dock never over capacity --
    try:
        res, raw = _plan("logistics_partial_ok.yaml")
        oracle = _oracle(raw)
        oracle.replay(res.model_dump()["nodes"], res.execution_order)
        ok = res.feasible and oracle.max_held["dock"] == 1
        checks.append(Check(
            "resource", "two interleaved shipments share capacity-1 dock safely",
            ok, f"leaves={len(res.execution_order)}, max_held={oracle.max_held}",
        ))
    except OracleFailure as exc:
        checks.append(Check("resource", "shared dock feasibility", False, str(exc)))
    except Exception as exc:  # noqa: BLE001
        not_executed.append(f"resource: {exc}")

    # 4. Partial-order conflict: topo orders exist but none execute ---------
    try:
        res, _ = _plan("logistics_partial_conflict.yaml")
        kinds = [f.kind.value for f in res.failures]
        ok = (
            not res.feasible
            and "partial_order_infeasible" in kinds
            and kinds.count("precondition_not_stat") == 2
        )
        checks.append(Check(
            "partial-order",
            "two token consumers: topological orders exist but neither executes",
            ok, f"kinds={sorted(set(kinds))}",
        ))
    except Exception as exc:  # noqa: BLE001
        not_executed.append(f"partial-order/token: {exc}")

    try:
        res, _ = _plan("logistics_resource_conflict.yaml")
        kinds = [f.kind.value for f in res.failures]
        ok = (
            not res.feasible
            and "partial_order_infeasible" in kinds
            and "resource_unavailable" in kinds
        )
        checks.append(Check(
            "partial-order",
            "two dock grabs: second ready sibling always exhausts resource",
            ok, f"kinds={sorted(set(kinds))}",
        ))
    except Exception as exc:  # noqa: BLE001
        not_executed.append(f"partial-order/resource: {exc}")

    # Failure category matrix: each infeasible fixture names a concrete class
    expected_categories = {
        "logistics_no_viable.yaml": "no_viable_method",
        "logistics_unresolvable.yaml": "unresolvable_task",
        "logistics_precondition_fail.yaml": "precondition_not_stat",
        "logistics_cycle.yaml": "method_cycle",
        "assembly_depth_capped.yaml": "depth_exceeded",
        "assembly_budget_tight.yaml": "expansion_budget_exhausted",
    }
    for fixture, terminal in expected_categories.items():
        try:
            res, _ = _plan(fixture)
            actual = res.failures[-1].kind.value if res.failures else None
            checks.append(Check(
                "failure-category", f"{fixture} -> {terminal}",
                (not res.feasible) and actual == terminal,
                f"actual={actual}",
            ))
        except Exception as exc:  # noqa: BLE001
            not_executed.append(f"failure-category/{fixture}: {exc}")

    # Hierarchy constraint: retained tree is connected root->leaves ---------
    try:
        res, _ = _plan("assembly_deep.yaml")

        def walk(nid):
            yield nid
            for c in res.nodes[nid].children:
                yield from walk(c)

        reachable = set(walk(res.roots[0]))
        ok = reachable == set(res.nodes) and all(
            res.nodes[n].status == "executable"
            for n in res.execution_order
        )
        checks.append(Check(
            "hierarchy", "retained tree reachable root->leaf; leaves executable",
            ok, f"nodes={len(res.nodes)}, leaves={len(res.execution_order)}",
        ))
    except Exception as exc:  # noqa: BLE001
        not_executed.append(f"hierarchy: {exc}")

    # -- report -------------------------------------------------------------
    width = max(len(c.name) for c in checks)
    print("=" * 78)
    print("FINITE HTN PLANNER — ACCEPTANCE VERIFICATION")
    print("=" * 78)
    failed = 0
    for c in checks:
        status = "PASS" if c.passed else "FAIL"
        if not c.passed:
            failed += 1
        print(f"[{status}] {c.dimension:15s} {c.name:<{width}}  ({c.detail})")

    if not_executed:
        print("\nNOT EXECUTED (reported separately, never counted as passed):")
        for item in not_executed:
            print(f"  - {item}")

    total = len(checks)
    passed = total - failed
    print("-" * 78)
    print(f"total={total} passed={passed} failed={failed} "
          f"not_executed={len(not_executed)}")
    print("=" * 78)
    return 1 if failed or not_executed else 0


if __name__ == "__main__":
    raise SystemExit(main())
