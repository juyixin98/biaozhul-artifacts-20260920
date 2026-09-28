"""Verification harness: kernel vs. independent brute-force enumeration.

For every fixture this script checks, and logs with an explicit basis:
1. solver status / solution set equals the reference enumeration
2. hand-written expectations in the fixture (NOT produced by the kernel)
3. propagation soundness: no pruned (var, value) at any search node occurs
   in any reference solution consistent with that node's partial assignment
4. fixture-specific expectations (Hall certificate, untouched domains,
   backtrack count, depth, root prunings)

Exit code 0 iff every check on every fixture passes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from csp_service.config import Settings
from csp_service.enumerate_ref import enumerate_solutions
from csp_service.kernel.solver import SAT, UNSAT, SolveConfig, Solver
from csp_service.model import Problem
from csp_service.runlog import RunLogger, problem_fingerprint

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _node_partials(events: list[dict]) -> dict[int, dict[str, int]]:
    """Reconstruct each node's partial assignment from branch events."""
    branch: dict[int, tuple[int, str, int]] = {}
    for e in events:
        if e["kind"] == "branch":
            branch[e["node"]] = (e["parent"], e["var"], e["value"])
    cache: dict[int, dict[str, int]] = {}

    def partial(node: int) -> dict[str, int]:
        if node in cache:
            return cache[node]
        if node not in branch:
            cache[node] = {}
        else:
            parent, var, value = branch[node]
            cache[node] = {**partial(parent), var: value}
        return cache[node]

    for node in [e.get("node") for e in events if "node" in e]:
        partial(node)
    return cache


def check_propagation_soundness(
    events: list[dict], reference: list[dict[str, int]]
) -> list[dict]:
    """Return a list of violations (empty = sound)."""
    partials = _node_partials(events)
    violations = []
    for e in events:
        if e["kind"] != "prune":
            continue
        partial = partials.get(e["node"], {})
        for sol in reference:
            if all(sol[k] == v for k, v in partial.items()) and sol[e["var"]] == e["value"]:
                violations.append(
                    {"event_seq": e["seq"], "var": e["var"], "value": e["value"],
                     "node": e["node"], "witness_solution": sol}
                )
                break
    return violations


def verify_fixture(path: Path, settings: Settings, logger: RunLogger) -> bool:
    fixture = json.loads(path.read_text())
    problem = Problem(**fixture["problem"])
    expect = fixture["expect"]
    fp = problem_fingerprint(fixture["problem"])
    logger.log("fixture_start", fixture=fixture["name"], problem_hash=fp,
               basis="loaded fixture file", file=path.name)

    ok = True

    def check(name: str, passed: bool, basis: str, **detail) -> None:
        nonlocal ok
        ok = ok and passed
        logger.log("check", fixture=fixture["name"], check=name,
                   verdict="pass" if passed else "FAIL", basis=basis, **detail)

    # 1. Independent reference enumeration (shares no code with the kernel).
    reference, complete = enumerate_solutions(
        problem, max_assignments=settings.enumeration_max_assignments
    )
    check("reference_enumeration_complete", complete,
          "brute-force enumeration must cover the whole assignment space",
          reference_count=len(reference))

    # 2. Kernel run, all solutions.
    result = Solver(problem, SolveConfig(mode="all")).solve()
    kernel_set = {tuple(sorted(s.items())) for s in result.solutions}
    ref_set = {tuple(sorted(s.items())) for s in reference}
    check("status_matches_expectation", result.status == expect["status"],
          f"fixture expects {expect['status']}: {expect['expectation_basis']}",
          actual=result.status)
    check("solution_set_matches_reference",
          kernel_set == ref_set,
          "kernel solution set must equal brute-force reference set",
          kernel_count=len(kernel_set), reference_count=len(ref_set))
    check("solution_count_matches_handwritten_expectation",
          len(result.solutions) == expect["solution_count"],
          expect["expectation_basis"],
          expected=expect["solution_count"], actual=len(result.solutions))

    # 3. Propagation soundness against the reference set.
    violations = check_propagation_soundness(result.events, reference)
    check("propagation_never_prunes_real_solution_values", not violations,
          "every pruned (var,value) must be absent from all reference solutions "
          "consistent with the node's partial assignment",
          violations=violations[:5])

    # 4. Fixture-specific expectations.
    root_prunes = sorted(
        [e["var"], e["value"]] for e in result.events
        if e["kind"] == "prune" and e["node"] == 0
    )
    if "root_prunings" in expect:
        check("root_prunings_match", root_prunes == sorted(expect["root_prunings"]),
              expect["expectation_basis"],
              expected=expect["root_prunings"], actual=root_prunes)
    if "nodes" in expect:
        check("search_nodes_match", result.stats["nodes"] == expect["nodes"],
              expect["expectation_basis"],
              expected=expect["nodes"], actual=result.stats["nodes"])
    if "min_backtracks" in expect:
        check("backtracks_at_least",
              result.stats["backtracks"] >= expect["min_backtracks"],
              expect["expectation_basis"],
              expected_min=expect["min_backtracks"],
              actual=result.stats["backtracks"])
    if "min_max_depth" in expect:
        check("max_depth_at_least",
              result.stats["max_depth"] >= expect["min_max_depth"],
              expect["expectation_basis"],
              expected_min=expect["min_max_depth"],
              actual=result.stats["max_depth"])
    if "hall_violation" in expect:
        halls = [e for e in result.events if e["kind"] == "hall_violation"]
        got = {"vars": halls[0]["vars"], "values": halls[0]["values"]} if halls else None
        check("hall_certificate_matches", got == expect["hall_violation"],
              expect["expectation_basis"],
              expected=expect["hall_violation"], actual=got)
    if "untouched_domains" in expect:
        for var, expected_domain in expect["untouched_domains"].items():
            pruned = [e["value"] for e in result.events
                      if e["kind"] == "prune" and e["var"] == var]
            check(f"isolated_var_{var}_untouched", not pruned,
                  f"{var} appears in no constraint; propagation must not touch it",
                  expected_domain=expected_domain, pruned=pruned)

    logger.log("fixture_done", fixture=fixture["name"], verdict="pass" if ok else "FAIL")
    return ok


def main() -> int:
    settings = Settings.from_env()
    logger = RunLogger(settings.log_dir)
    fixture_paths = sorted(FIXTURES_DIR.glob("*.json"))
    print(f"verification run_id={logger.run_id} fixtures={len(fixture_paths)}")
    all_ok = True
    for path in fixture_paths:
        ok = verify_fixture(path, settings, logger)
        all_ok = all_ok and ok
        print(f"  {path.name}: {'PASS' if ok else 'FAIL'}")
    logger.log("run_done", verdict="pass" if all_ok else "FAIL")
    print(f"log: {logger.path}")
    print("VERDICT:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
