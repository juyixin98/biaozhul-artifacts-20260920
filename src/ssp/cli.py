"""Command-line interface: run a reproducible planning experiment.

Examples
--------
# Replay every shipped fixture and print the plan + evidence table:
python -m ssp.cli fixtures --trials 8000

# One-off normal plan:
python -m ssp.cli normal --alternative two_sided --alpha 0.05 \\
    --power 0.8 --effect 0.5 --scale standardized_d --trials 8000

# One-off exact binomial plan at a low base rate:
python -m ssp.cli binomial --alternative greater --alpha 0.05 \\
    --power 0.8 --p0 0.01 --effect 0.03 --scale proportions
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .api.schemas import BinomialPlanRequest, NormalPlanRequest
from .api.service import build_binomial_spec, build_normal_spec
from .config import get_settings
from .diagnostics import RunLogger, input_fingerprint, new_run_id
from .errors import PlannerError
from .repro import ExperimentRunner, list_fixtures, load_fixture
from .storage import RunStore


def _print_json(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def _run_one(
    request: dict,
    trials: int | None,
    run_id: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    settings = get_settings()
    store = RunStore(settings)
    if persist:
        store.initialize()
    runner = ExperimentRunner(settings)
    run_id = run_id or new_run_id()
    fp = input_fingerprint(request)
    logger = RunLogger(run_id, fp)
    if request["endpoint"] == "normal":
        req = NormalPlanRequest(**{**request, "mc_trials": trials})
        spec = build_normal_spec(req)
        record = runner.run_normal(spec, run_id, fp, logger, trials=trials)
    else:
        req = BinomialPlanRequest(**{**request, "mc_trials": trials})
        spec = build_binomial_spec(req)
        record = runner.run_binomial(spec, run_id, fp, logger, trials=trials)
    if persist:
        store.save_success(record.to_dict(), request)
    return record.to_dict()


def _cmd_normal(args: argparse.Namespace) -> int:
    request = {
        "endpoint": "normal",
        "alternative": args.alternative,
        "alpha": args.alpha,
        "target_power": args.power,
        "effect": args.effect,
        "effect_scale": args.scale,
        "two_sample": args.two_sample,
        "sigma": args.sigma,
        "known_sigma": args.known_sigma,
        "allocation_ratio": args.allocation_ratio,
        "interim_looks": 1,
        "method_preference": args.method,
    }
    return _execute(request, args.trials)


def _cmd_binomial(args: argparse.Namespace) -> int:
    request = {
        "endpoint": "binomial",
        "alternative": args.alternative,
        "alpha": args.alpha,
        "target_power": args.power,
        "p0": args.p0,
        "effect": args.effect,
        "effect_scale": args.scale,
        "two_sample": args.two_sample,
        "allocation_ratio": args.allocation_ratio,
        "interim_looks": 1,
        "method_preference": args.method,
    }
    return _execute(request, args.trials)


def _execute(request: dict, trials: int | None) -> int:
    try:
        _print_json(_run_one(request, trials))
        return 0
    except PlannerError as exc:
        sys.stderr.write(json.dumps(exc.to_dict(), indent=2) + "\n")
        return 2


def _cmd_fixtures(args: argparse.Namespace) -> int:
    settings = get_settings()
    names = list_fixtures(settings)
    failures = 0
    for index, name in enumerate(names):
        fixture = load_fixture(name, settings)
        request = fixture["request"]
        # Deterministic run id per fixture -> deterministic RNG seed, so a
        # replay is bit-for-bit reproducible (same row replaced on rerun).
        run_id = f"fixture-{index:02d}-{name.removesuffix('.json')}"
        try:
            record = _run_one(request, args.trials, run_id=run_id)
        except PlannerError as exc:
            print(f"[FAIL] {name}: {exc.category.value} - {exc}")
            failures += 1
            continue
        plan = record["plan"]
        agree = record["agreement"]["agrees"]
        allowed_gap = "approximation_allowance" in fixture.get("reference", {})
        if agree:
            marker = "OK  "
        elif allowed_gap:
            marker = "APRX"  # documented approximation gap, not a failure
        else:
            marker = "DIFF"
        print(
            f"[{marker}] {name:38s} "
            f"method={plan['method']:32s} allocation={plan['allocation']} "
            f"power={plan['achieved_power']:.4f} "
            f"n-1={plan['minimal_integer_check']['power_at_total_minus_one']:.4f} "
            f"sim={record['evidence']['estimated_power']:.4f}"
        )
        if not agree and not allowed_gap:
            failures += 1
    print(f"\n{len(list_fixtures(settings)) - failures} OK, {failures} flagged")
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ssp", description="Sample size planning experiments")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--alternative", choices=["two_sided", "greater", "less"], required=True)
        p.add_argument("--alpha", type=float, default=0.05)
        p.add_argument("--power", dest="power", type=float, default=0.8)
        p.add_argument("--two-sample", action="store_true")
        p.add_argument("--allocation-ratio", type=float, default=1.0)
        p.add_argument("--method", choices=["auto", "asymptotic", "exact"], default="auto")
        p.add_argument("--trials", type=int, default=None)

    pn = sub.add_parser("normal", help="normal-endpoint plan")
    add_common(pn)
    pn.add_argument("--effect", type=float, required=True)
    pn.add_argument("--scale", choices=["absolute_difference", "standardized_d"],
                    default="standardized_d")
    pn.add_argument("--sigma", type=float, default=None)
    pn.add_argument("--known-sigma", action="store_true")
    pn.set_defaults(func=_cmd_normal)

    pb = sub.add_parser("binomial", help="binomial-endpoint plan")
    add_common(pb)
    pb.add_argument("--p0", type=float, required=True)
    pb.add_argument("--effect", type=float, required=True)
    pb.add_argument("--scale",
                    choices=["risk_difference", "relative_risk", "odds_ratio", "proportions"],
                    default="proportions")
    pb.set_defaults(func=_cmd_binomial)

    pf = sub.add_parser("fixtures", help="replay all synthetic fixtures")
    pf.add_argument("--trials", type=int, default=6000)
    pf.set_defaults(func=_cmd_fixtures)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
