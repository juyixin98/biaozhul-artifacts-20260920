#!/usr/bin/env python3
"""End-to-end verification harness over the reusable fixtures.

For every fixture case this:
  1. parses the domain and problem with the rule language,
  2. runs the bounded planner (using the case's bounds, if any),
  3. runs the INDEPENDENT verifier (replay + hierarchy + linearization +
     failure corroboration),
  4. compares concrete outcomes against the HAND-WRITTEN expected file.

It prints a per-case report and exits non-zero if any check fails.  Checks
that cannot be executed in this offline environment are listed separately in
the report rather than silently treated as passing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from htn_planner.core.engine import Bounds, Planner  # noqa: E402
from htn_planner.lang import LangError, parse_domain, parse_problem  # noqa: E402
from htn_planner.verify import Verifier  # noqa: E402

FIXTURES = ROOT / "fixtures"
DOMAINS = FIXTURES / "domains"
PROBLEMS = FIXTURES / "problems"
EXPECTED = FIXTURES / "expected"

# Checks that are deliberately NOT run offline; reported, never faked.
NOT_EXECUTED = [
    {
        "check": "remote/authenticated service deployment",
        "reason": "no production accounts or network services are in scope;",
        "detail": "only the local ASGI app and local SQLite are exercised.",
    },
    {
        "check": "load/performance testing at scale",
        "reason": "synthetic fixtures are small; search is bounded on purpose.",
        "detail": "bounds are configurable per request for future benchmarking.",
    },
]


def _load_case(expected_name: str) -> dict:
    expected = json.loads((EXPECTED / expected_name).read_text())
    case = expected["case"]
    mapping = {
        "logistics_direct": ("logistics.htn", "logistics_direct.pddl"),
        "logistics_recursive": ("logistics.htn", "logistics_recursive.pddl"),
        "logistics_ship": ("logistics.htn", "logistics_ship.pddl"),
        "logistics_unreachable": ("logistics.htn", "logistics_unreachable.pddl"),
        "docks_two": ("docks.htn", "docks_two.pddl"),
        "docks_three": ("docks.htn", "docks_three.pddl"),
        "permit_two": ("permit_deadlock.htn", "permit_two.pddl"),
        "bounds_spin": ("bounds.htn", "bounds_spin.pddl"),
        "bounds_walk_long": ("bounds.htn", "bounds_walk_long.pddl"),
    }
    dom_file, prob_file = mapping[case]
    domain = parse_domain((DOMAINS / dom_file).read_text())
    problem = parse_problem((PROBLEMS / prob_file).read_text())
    bounds = Bounds(**(expected.get("bounds") or {"max_depth": 12}))
    return {"expected": expected, "domain": domain, "problem": problem, "bounds": bounds}


def _check_actions(result: object, expected: dict, failures: list[str]) -> None:
    if "expected_actions" not in expected:
        return
    actual = [[a.operator, list(a.args)] for a in result.plan]  # type: ignore[attr-defined]
    if actual != expected["expected_actions"]:
        failures.append(
            f"actions mismatch: expected {expected['expected_actions']}, got {actual}"
        )


def _check_failures(result: object, expected: dict, failures: list[str]) -> None:
    cats = sorted({f.category for f in result.failures})  # type: ignore[attr-defined]
    want = sorted(expected.get("expected_failure_categories", []))
    if cats != want:
        failures.append(f"failure categories: expected {want}, got {cats}")
    if "expected_uncertain_categories" in expected:
        uncertain = sorted({u.category for u in result.uncertain})  # type: ignore[attr-defined]
        if uncertain != sorted(expected["expected_uncertain_categories"]):
            failures.append(
                f"uncertain categories: expected"
                f" {expected['expected_uncertain_categories']}, got {uncertain}"
            )


def _check_verification(report: object, expected: dict, failures: list[str]) -> None:
    want = expected.get("verification", {})
    if "ok" in want and report.ok != want["ok"]:  # type: ignore[attr-defined]
        failures.append(f"verification ok: expected {want['ok']}, got {report.ok}")  # type: ignore[attr-defined]
    for key in ("executable", "hierarchy_consistent", "order_consistent", "failure_sound"):
        if key in want and getattr(report, key) != want[key]:
            failures.append(f"verification {key}: expected {want[key]}, got {getattr(report, key)}")
    if "feasible_linearizations" in want:
        got = len(report.feasible_linearizations)  # type: ignore[attr-defined]
        if got != want["feasible_linearizations"]:
            failures.append(
                f"feasible linearizations: expected"
                f" {want['feasible_linearizations']}, got {got}"
            )
    if "linearizations_examined" in want:
        got = report.linearizations_examined  # type: ignore[attr-defined]
        if got != want["linearizations_examined"]:
            failures.append(
                f"linearizations examined: expected"
                f" {want['linearizations_examined']}, got {got}"
            )
    if "expected_violation_code" in want:
        codes = {v.code for v in report.violations}  # type: ignore[attr-defined]
        if want["expected_violation_code"] not in codes:
            failures.append(
                f"expected violation code {want['expected_violation_code']}"
                f" in {sorted(codes)}"
            )


def _check_adequate_bounds(case: dict, failures: list[str]) -> None:
    expected = case["expected"]
    if "adequate_bounds" not in expected:
        return
    result = Planner(Bounds(**expected["adequate_bounds"])).solve(
        case["domain"], case["problem"], "verify-adequate"
    )
    if result.status != expected["adequate_expected_status"]:
        failures.append(
            f"adequate-bounds status: expected"
            f" {expected['adequate_expected_status']}, got {result.status}"
        )
    actual = [[a.operator, list(a.args)] for a in result.plan]
    if actual != expected["adequate_expected_actions"]:
        failures.append("adequate-bounds action mismatch")


def main() -> int:
    expected_files = sorted(p.name for p in EXPECTED.glob("*.json"))
    if not expected_files:
        print("No expected fixtures found.", file=sys.stderr)
        return 2

    verifier = Verifier()
    total_failures = 0
    print("Bounded HTN planner - fixture verification")
    print("=" * 72)
    for name in expected_files:
        failures: list[str] = []
        try:
            case = _load_case(name)
        except (LangError, KeyError, FileNotFoundError) as exc:
            print(f"[SETUP-ERROR] {name}: {exc}")
            total_failures += 1
            continue
        expected = case["expected"]
        result = Planner(case["bounds"]).solve(
            case["domain"], case["problem"], f"verify-{expected['case']}"
        )
        if result.status != expected["expected_status"]:
            failures.append(
                f"status: expected {expected['expected_status']}, got {result.status}"
            )
        _check_actions(result, expected, failures)
        _check_failures(result, expected, failures)
        report = verifier.verify(case["domain"], case["problem"], result)
        _check_verification(report, expected, failures)
        _check_adequate_bounds(case, failures)

        label = expected["case"]
        if failures:
            total_failures += 1
            print(f"[FAIL] {label}")
            for f in failures:
                print(f"        - {f}")
        else:
            extra = ""
            if report.feasible_linearizations is not None and result.status == "success":
                extra = (
                    f" | feasible linearizations"
                    f" {len(report.feasible_linearizations)}/{report.linearizations_examined}"
                )
            print(
                f"[PASS] {label:28s} status={result.status:12s}"
                f" actions={len(result.plan)}{extra}"
            )

    print("=" * 72)
    print("Checks NOT executed in this offline environment (not counted as pass):")
    for item in NOT_EXECUTED:
        print(f"  - {item['check']}: {item['reason']} {item['detail']}")

    if total_failures:
        print(f"\nRESULT: {total_failures} case(s) FAILED")
        return 1
    print(f"\nRESULT: all {len(expected_files)} fixture cases PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
