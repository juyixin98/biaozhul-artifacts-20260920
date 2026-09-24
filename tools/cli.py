#!/usr/bin/env python3
"""Offline SOC estimation CLI.

Examples:
    # run the built-in mixed scenario and print a summary
    python tools/cli.py --scenario mixed

    # dump a built-in scenario to JSON (example input)
    python tools/cli.py --scenario sensor_dropout --dump-json examples/sensor_dropout.json

    # estimate from a JSON file: {"initial_soc": ..., "samples": [...]}
    python tools/cli.py --input examples/mixed.json

    # verify the frozen parameter signature
    python tools/cli.py --verify-params
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.estimator import Sample, estimate_soc  # noqa: E402
from app.params import ParamsIntegrityError, load_params  # noqa: E402
from app.synthetic import (  # noqa: E402
    scenario_bias_drift,
    scenario_charge_discharge,
    scenario_mixed,
    scenario_out_of_table_temp,
    scenario_sensor_dropout,
)

SCENARIOS = {
    "charge_discharge": scenario_charge_discharge,
    "bias_drift": scenario_bias_drift,
    "out_of_table_temp": scenario_out_of_table_temp,
    "sensor_dropout": scenario_sensor_dropout,
    "mixed": scenario_mixed,
}


def samples_to_json(samples: list[Sample], initial_soc=None) -> dict:
    return {
        "initial_soc": initial_soc,
        "samples": [
            {"t_s": s.t_s, "current_a": s.current_a, "voltage_v": s.voltage_v, "temp_c": s.temp_c}
            for s in samples
        ],
    }


def summarize(result) -> dict:
    last = result.points[-1] if result.points else None
    return {
        "n_samples": result.n_samples,
        "n_integrated_intervals": result.n_integrated_intervals,
        "n_gap_intervals": result.n_gap_intervals,
        "final_soc": result.final_soc,
        "final_soc_sigma": result.final_soc_sigma,
        "flags": result.flags,
        "calibrations": [vars(c) for c in result.calibrations],
        "gaps": [vars(g) for g in result.gaps],
        "last_point": vars(last) if last else None,
        "not_for_control": (
            "EXPERIMENTAL SYNTHETIC-MODEL OUTPUT - NOT FOR REAL CHARGE CONTROL"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=sorted(SCENARIOS))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--initial-soc", type=float, default=None)
    parser.add_argument("--dump-json", type=Path)
    parser.add_argument("--verify-params", action="store_true")
    args = parser.parse_args()

    try:
        params = load_params()
    except ParamsIntegrityError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    print(f"params {params.version} sha256={params.digest[:16]}... (signature verified)")

    if args.verify_params:
        print("parameter signature VALID")
        return 0

    if args.input:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        initial = payload.get("initial_soc", args.initial_soc)
        samples = [Sample(**s) for s in payload["samples"]]
        scenario_name = args.input.name
    elif args.scenario:
        synth = SCENARIOS[args.scenario](params)
        samples = synth.samples
        initial = args.initial_soc
        scenario_name = args.scenario
        if args.dump_json:
            args.dump_json.parent.mkdir(parents=True, exist_ok=True)
            args.dump_json.write_text(
                json.dumps(samples_to_json(samples, args.initial_soc), indent=2),
                encoding="utf-8",
            )
            print(f"wrote {args.dump_json} ({len(samples)} samples)")
            return 0
    else:
        parser.error("one of --scenario or --input is required")

    result = estimate_soc(samples, params, initial)
    summary = summarize(result)
    summary["input"] = scenario_name
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
