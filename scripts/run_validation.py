#!/usr/bin/env python
"""Reproducible validation run.

Executes the four evidence layers in one run and writes a JSONL trace plus a
human-readable report under logs/runs/, keyed by a single run id:

  1. analytic special cases (closed-form normal / hand-derived exact case);
  2. integer-boundary witness (power(n) >= target, power(n-1) < target);
  3. independent Monte Carlo agreement;
  4. committed group-sequential boundaries and commitment guards.

Usage:
    python scripts/run_validation.py [--replications 40000]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sample_size_planner.api.schemas import (  # noqa: E402
    BinomialPlanRequest, InterimPlanRequest, NormalPlanRequest, SimulationRequest,
)
from sample_size_planner.api.service import PlanningService  # noqa: E402
from sample_size_planner.config import Settings  # noqa: E402
from sample_size_planner.contracts import TestDirection  # noqa: E402
from sample_size_planner.diagnostics import interim as im  # noqa: E402
from sample_size_planner.evidence.run_log import RunIdentity, RunLogger  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replications", type=int, default=40_000)
    parser.add_argument("--seed", type=int, default=20260927)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    settings = Settings(
        db_path=root / "data" / "planner.db",
        runs_dir=root / "logs" / "runs",
        log_dir=root / "logs",
    )
    settings.ensure_dirs()
    service = PlanningService(settings)
    run = RunIdentity.new("validation")
    logger = RunLogger(run, settings.runs_dir / f"{run.run_id}.jsonl")
    logger.event("validation_started", versions=run.versions, args=vars(args))

    report: list[dict] = []

    def record(title: str, response: dict, *, expect_success: bool = True) -> bool:
        ok = bool(response.get("success")) is expect_success
        line = {"title": title, "ok": ok, "run_id": response.get("run_id"),
                "success": response.get("success"),
                "failure_category": response.get("failure_category"),
                "n0": response.get("n_per_group0"), "n1": response.get("n_per_group1"),
                "power": response.get("achieved_power"),
                "power_n_minus_one": response.get("power_at_n_minus_one"),
                "method": response.get("method")}
        report.append(line)
        logger.judgement("accept" if ok else "failure", title, **line)
        return ok

    # 1-2. analytic plans + integer boundary
    normal = service.plan_normal(NormalPlanRequest(
        alpha=0.05, power=0.8, direction=TestDirection.TWO_SIDED,
        standardized_effect=0.8), logger)
    record("normal d=0.8 two-sided -> 25/group", normal)

    binom = service.plan_binomial(BinomialPlanRequest(
        alpha=0.05, power=0.8, direction=TestDirection.GREATER,
        p0=0.001, p1=0.01), logger)
    record("binomial low-rate 0.001 vs 0.01 -> exact", binom)

    zero = service.plan_normal(NormalPlanRequest(
        alpha=0.05, power=0.8, direction=TestDirection.TWO_SIDED,
        standardized_effect=0.0), logger)
    record("zero effect -> effect_zero failure", zero, expect_success=False)

    # 3. independent Monte Carlo agreement with the planned normal size
    sim = service.run_simulation(SimulationRequest(
        family="normal", n_per_group0=normal["n_per_group0"],
        n_per_group1=normal["n_per_group1"], alpha=0.05,
        direction=TestDirection.TWO_SIDED, standardized_effect=0.8,
        replications=args.replications, seed=args.seed,
        target_power=normal["achieved_power"]), logger)
    ok_sim = bool(sim["agrees_with_target"])
    report.append({"title": "MC vs analytic normal power", "ok": ok_sim,
                   "estimated": sim["estimated_power"], "ci95": sim["ci95"],
                   "target": sim["target_power"], "seed": sim["seed"]})
    logger.judgement("accept" if ok_sim else "reject", "MC cross-check", **report[-1])

    # 4. committed interim schedule and the guard against an unplanned look
    interim = service.plan_interim(InterimPlanRequest(
        information_times=[0.5, 1.0], alpha=0.05, direction=TestDirection.TWO_SIDED,
        family="pocock", drift_at_full_information=2.8), logger)
    ok_interim = interim.get("z_boundaries") is not None
    report.append({"title": "Pocock K=2 two-sided boundary", "ok": ok_interim,
                   "boundaries": interim.get("z_boundaries"),
                   "final_size": (interim.get("cumulative_alpha_spent") or [None])[-1]})
    logger.judgement("accept" if ok_interim else "failure", "interim", **report[-1])

    guard_ok = False
    try:
        schedule = im.build_schedule([0.5, 1.0], alpha=0.05, two_sided=True,
                                     family=im.BoundaryFamily.POCOCK)
        im.verify_look_is_committed(schedule, 0.33)
    except im.InterimError as exc:
        guard_ok = exc.category.value == "interim_look_outside_commitment"
    report.append({"title": "unplanned look rejected", "ok": guard_ok})
    logger.judgement("accept" if guard_ok else "failure", "interim guard",
                     ok=guard_ok)

    all_ok = all(item["ok"] for item in report)
    summary_path = settings.runs_dir / f"{run.run_id}-report.json"
    summary_path.write_text(json.dumps(
        {"run_id": run.run_id, "versions": run.versions,
         "all_passed": all_ok, "checks": report}, indent=2, default=str))

    print(f"\nValidation run {run.run_id}")
    print(f"Python {run.versions['python']} | numpy {run.versions['numpy']} | "
          f"scipy {run.versions['scipy']} | fastapi {run.versions['fastapi']}")
    for item in report:
        print(f"  [{'PASS' if item['ok'] else 'FAIL'}] {item['title']}")
    print(f"\nReport: {summary_path}")
    print(f"Trace : {settings.runs_dir / (run.run_id + '.jsonl')}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
