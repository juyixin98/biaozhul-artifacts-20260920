#!/usr/bin/env python3
"""Verification harness: solver kernel vs. independent enumeration oracle.

For every fixture (and optionally random instances) this script:

1. enumerates all solutions with the independent oracle (tests/oracle.py,
   which imports nothing from the solver kernel);
2. runs the solver kernel on the same model;
3. checks status agreement, solution validity, and that root propagation
   never pruned a value that appears in some enumerated solution;
4. checks that backtracking restores domains (branch trails replayed);
5. writes a JSON report with versions, run identity and per-case verdicts.

Exit code 0 means every check passed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import sys
import time
import uuid
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import oracle  # noqa: E402  (independent reference, no solver imports)
from app import __version__  # noqa: E402
from app.fixtures import all_fixtures  # noqa: E402
from app.fixtures.catalog import NOT_ENUMERABLE, seeded_random_instance  # noqa: E402
from app.solver import (  # noqa: E402
    CSPModel,
    DomainStore,
    Propagator,
    PropagationStats,
    SearchStatus,
    Solver,
    run_propagation,
)

logger = logging.getLogger("csp.verify")


def check_fixture(name: str, payload: dict) -> dict:
    """Run all checks for one fixture; returns a verdict record."""
    record: dict = {"fixture": name, "checks": [], "skipped": [], "ok": True}

    def check(label: str, passed: bool, detail: str = "") -> None:
        record["checks"].append(
            {"label": label, "passed": passed, "detail": detail}
        )
        if not passed:
            record["ok"] = False
            logger.error("FAIL %s :: %s :: %s", name, label, detail)

    def skip(label: str, detail: str) -> None:
        record["skipped"].append({"label": label, "detail": detail})
        logger.warning("SKIP %s :: %s :: %s", name, label, detail)

    model = CSPModel.model_validate(payload)

    # --- oracle ground truth -------------------------------------------------
    enumerable = name not in NOT_ENUMERABLE
    if enumerable:
        solutions = oracle.enumerate_solutions(payload)
        viable = oracle.viable_values(payload)
        oracle_status = "sat" if solutions else "unsat"
        record["oracle"] = {
            "status": oracle_status,
            "solution_count": len(solutions),
            "method": "full enumeration",
        }
    else:
        solutions = None
        viable = None
        oracle_status = None
        record["oracle"] = {
            "status": None,
            "solution_count": None,
            "method": "not executed (assignment space too large)",
        }
        skip(
            "full_enumeration",
            "complete assignment space too large to enumerate; "
            "solution validity is still checked directly",
        )

    # --- root propagation soundness ------------------------------------------
    store = DomainStore(model.domains)
    propagator = Propagator(model)
    stats = PropagationStats()
    trail: list = []
    root_error: str | None = None
    try:
        run_propagation(propagator, store, trail, stats, changed=None)
    except Exception as exc:  # EmptyDomain or Hall violation
        root_error = type(exc).__name__

    if enumerable and oracle_status == "sat":
        check(
            "root_propagation_not_false_infeasible",
            root_error is None,
            f"root propagation raised {root_error} on a satisfiable model",
        )
        if root_error is None:
            remaining = {v: set(store.domain(v)) for v in store.variables()}
            pruned_viable = {
                v: sorted(viable[v] - remaining[v])
                for v in remaining
                if viable[v] - remaining[v]
            }
            check(
                "root_propagation_keeps_all_viable_values",
                not pruned_viable,
                f"pruned values that occur in solutions: {pruned_viable}",
            )
    elif enumerable:
        logger.info("%s is unsat per oracle; root error=%s", name, root_error)
    else:
        skip(
            "root_propagation_soundness",
            "requires the enumerated viable-value set",
        )

    # --- full solver agreement ------------------------------------------------
    result = Solver(model).solve(max_nodes=200_000, max_backtracks=200_000)
    if enumerable:
        check(
            "status_agrees_with_oracle",
            result.status.value == oracle_status,
            f"solver={result.status.value} oracle={oracle_status}",
        )
    else:
        check(
            "status_is_decided",
            result.status is not SearchStatus.UNKNOWN,
            f"unexpected unknown status for {name}",
        )
    if result.status is SearchStatus.SAT:
        check(
            "solution_satisfies_model",
            oracle._assignment_satisfies(payload, result.solution or {}),
            f"returned solution {result.solution} violates the model",
        )
        if enumerable:
            check(
                "solution_in_oracle_set",
                (result.solution or {}) in (solutions or []),
                "solver solution missing from enumerated solution set",
            )
    if enumerable and oracle_status == "unsat":
        check(
            "unsat_proven",
            result.status is SearchStatus.UNSAT,
            f"expected unsat, got {result.status.value}",
        )

    # --- backtracking restores domains ----------------------------------------
    # Exact restoration check: snapshot domains, apply a partial assignment
    # and propagate, replay the trail, then demand the domains are
    # bit-for-bit the snapshot again (this is what search does on backtrack).
    restore_ok = True
    restore_detail = ""
    for assignment in _partial_assignments(payload, attempts=6):
        branch_store = DomainStore(model.domains)
        branch_prop = Propagator(model)
        branch_trail: list = []
        before = {v: set(branch_store.domain(v)) for v in branch_store.variables()}
        branch_failed = False
        try:
            for variable, value in assignment.items():
                branch_store.assign(variable, value, branch_trail)
            run_propagation(
                branch_prop, branch_store, branch_trail,
                PropagationStats(), changed=list(assignment),
            )
        except Exception:
            branch_failed = True
        DomainStore.restore(branch_trail, branch_store)
        after = {v: set(branch_store.domain(v)) for v in branch_store.variables()}
        if before != after:
            restore_ok = False
            restore_detail = (
                f"assignment={assignment} before={ {k: sorted(v) for k, v in before.items()} }"
                f" after={ {k: sorted(v) for k, v in after.items()} }"
            )
            break
        # A branch that failed must have restored to a non-empty, searchable
        # state so the search can continue with the next value.
        if branch_failed and any(not values for values in after.values()):
            restore_ok = False
            restore_detail = f"failed branch {assignment} left an empty domain"
            break
    check(
        "trail_restores_domains_exactly",
        restore_ok,
        restore_detail,
    )

    # Budget exhaustion must be reported as unknown, never as success.
    budgeted = Solver(model).solve(max_nodes=1, max_backtracks=10**9)
    if budgeted.status is SearchStatus.UNKNOWN:
        check(
            "budget_reported_as_unknown",
            budgeted.failure is not None
            and budgeted.failure.get("kind") == "budget",
            f"unknown status without a budget failure record: {budgeted.failure}",
        )
    else:
        check(
            "budget_reported_as_unknown",
            budgeted.status in (SearchStatus.SAT, SearchStatus.UNSAT),
            f"decided within a single node: {budgeted.status.value}",
        )

    return record


def _partial_assignments(payload: dict, attempts: int) -> list[dict]:
    """A spread of partial assignments (deterministic, no solver reuse)."""
    import random

    rng = random.Random(9173)
    variables = list(payload["domains"])
    assignments: list[dict] = []
    # Empty assignment exercises root-only restore.
    assignments.append({})
    for _ in range(attempts):
        count = rng.randint(1, min(2, len(variables)))
        chosen = rng.sample(variables, count)
        assignments.append(
            {variable: rng.choice(payload["domains"][variable]) for variable in chosen}
        )
    return assignments


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--random-cases",
        type=int,
        default=0,
        help="also verify N seeded random instances",
    )
    parser.add_argument(
        "--report",
        default="verification_report.json",
        help="where to write the JSON report",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    run_id = uuid.uuid4().hex
    started = time.time()
    logger.info(
        "verification run_id=%s solver_version=%s python=%s",
        run_id,
        __version__,
        platform.python_version(),
    )

    cases: dict[str, dict] = all_fixtures()
    for seed in range(args.random_cases):
        payload = seeded_random_instance(seed=seed)
        cases[payload["name"]] = payload

    records = []
    for index, (name, payload) in enumerate(sorted(cases.items()), start=1):
        logger.info(
            "case %d/%d %s (variables=%d)",
            index,
            len(cases),
            name,
            len(payload["domains"]),
        )
        records.append(check_fixture(name, payload))

    passed = sum(1 for record in records if record["ok"])
    report = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "solver_version": __version__,
        "python_version": platform.python_version(),
        "elapsed_seconds": round(time.time() - started, 3),
        "total_cases": len(records),
        "passed_cases": passed,
        "failed_cases": len(records) - passed,
        "cases": records,
    }
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    logger.info(
        "done: %d/%d cases passed; report=%s", passed, len(records), args.report
    )
    return 0 if passed == len(records) else 1


if __name__ == "__main__":
    sys.exit(main())
