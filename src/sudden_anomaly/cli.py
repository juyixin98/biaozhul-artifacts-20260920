"""Command-line entry point for the offline signal anomaly service.

Examples (see examples/requests.md for the full list)::

    python -m sudden_anomaly.cli --scenario all --out-dir out
    python -m sudden_anomaly.cli --scenario step --block-size 137 --out-dir out
    python -m sudden_anomaly.cli --pcm data.pcm --pcm-format s16 \
        --sample-rate 100 --out-dir out
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional, Sequence

import numpy as np

from . import pcm_io
from .detector import DetectorConfig, detect_offline
from .evaluation import EvaluationReport, evaluate
from .signals import (
    SignalScenario,
    make_drift,
    make_missing,
    make_spikes,
    make_step,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m sudden_anomaly.cli",
        description="Causal sliding robust-statistics pulse anomaly detector "
        "(offline, numbers/files only).",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--scenario",
        choices=["step", "drift", "spikes", "all"],
        help="run a built-in synthetic scenario",
    )
    src.add_argument("--pcm", help="path to a raw PCM file")

    p.add_argument(
        "--pcm-format",
        choices=["s16", "s32", "float32", "float64", "uint8"],
        default="s16",
    )
    p.add_argument("--pcm-endian", choices=["little", "big"], default="little")
    p.add_argument(
        "--sample-rate", type=float, default=100.0,
        help="sample rate in Hz (synthetic and PCM), used for delay in seconds",
    )
    p.add_argument("--threshold", type=float, default=5.0)
    p.add_argument("--window-size", type=int, default=512)
    p.add_argument("--min-history", type=int, default=16)
    p.add_argument("--prime-size", type=int, default=64)
    p.add_argument(
        "--recent-size", type=int, default=32,
        help="recent sub-window length for the causal level-shift statistic",
    )
    p.add_argument("--recent-min", type=int, default=8)
    p.add_argument(
        "--shift-persist", type=int, default=3,
        help="consecutive threshold exceedances required for a shift alarm",
    )
    p.add_argument("--scale", choices=["mad", "std"], default="mad")
    p.add_argument("--center", choices=["median", "mean"], default="median")
    p.add_argument(
        "--block-size", type=int, default=None,
        help="feed the signal in blocks of this many samples (parity check mode)",
    )
    p.add_argument(
        "--missing-rate", type=float, default=0.0,
        help="(synthetic only) inject this fraction of NaN missing samples",
    )
    p.add_argument("--seed", type=int, default=1001)
    p.add_argument("--out-dir", default="out", help="directory for CSV/JSON files")
    p.add_argument(
        "--json", action="store_true",
        help="print the machine-readable JSON summary to stdout",
    )
    return p


def _scenarios(args: argparse.Namespace) -> List[SignalScenario]:
    if args.scenario == "step":
        builders = [make_step]
    elif args.scenario == "drift":
        builders = [make_drift]
    elif args.scenario == "spikes":
        builders = [make_spikes]
    else:
        builders = [make_step, make_drift, make_spikes]
    out: List[SignalScenario] = []
    for i, builder in enumerate(builders):
        sc = builder(sample_rate=args.sample_rate, seed=args.seed + i)
        if args.missing_rate > 0:
            sc = make_missing(sc, missing_rate=args.missing_rate, seed=args.seed + 100 + i)
        out.append(sc)
    return out


def _config_from_args(args: argparse.Namespace) -> DetectorConfig:
    return DetectorConfig(
        threshold=args.threshold,
        window_size=args.window_size,
        min_history=args.min_history,
        prime_size=args.prime_size,
        recent_size=args.recent_size,
        recent_min=args.recent_min,
        shift_persist=args.shift_persist,
        scale_estimator=args.scale,
        center=args.center,
    )


def _print_human(report: EvaluationReport) -> None:
    print(f"  scenario          : {report.scenario}")
    print(f"  samples           : {report.n_samples} (eligible: {report.n_eligible})")
    print(f"  anomaly decisions : {report.n_anomalies}")
    print(f"  false alarms      : {report.n_false_alarms} "
          f"({report.false_alarm_rate_per_1000:.4f}/1000 eligible)")
    print(f"  detection rate    : {report.detection_rate:.0%}")
    if report.mean_delay_samples is not None:
        print(
            f"  mean delay        : {report.mean_delay_samples:.2f} samples "
            f"= {report.mean_delay_seconds * 1000:.2f} ms"
        )
    for e in report.events:
        if e.detected:
            print(
                f"    - {e.kind:<5} @ {e.start:>5}: detected at {e.detection_index} "
                f"(delay {e.delay_samples} samples / {e.delay_seconds * 1000:.1f} ms)"
            )
        else:
            print(f"    - {e.kind:<5} @ {e.start:>5}: NOT detected")


def run_synthetic(args: argparse.Namespace) -> List[dict]:
    config = _config_from_args(args)
    summaries: List[dict] = []
    for sc in _scenarios(args):
        result = detect_offline(sc.signal, config, block_size=args.block_size)
        report = evaluate(sc, result)
        stem = sc.name
        pcm_io.write_point_csv(
            Path(args.out_dir) / f"{stem}_points.csv", result,
            sample_rate=sc.sample_rate, signal=sc.signal,
        )
        summary_path = Path(args.out_dir) / f"{stem}_summary.json"
        pcm_io.write_json_summary(
            summary_path, result=result, report=report,
            config=asdict(config),
            extra={"description": sc.description, "block_size": args.block_size},
        )
        _print_human(report)
        summaries.append(report.as_dict())
    return summaries


def run_pcm(args: argparse.Namespace) -> dict:
    config = _config_from_args(args)
    x, info = pcm_io.read_pcm(
        args.pcm, sample_format=args.pcm_format, sample_rate=args.sample_rate,
        endian=args.pcm_endian,
    )
    result = detect_offline(x, config, block_size=args.block_size)
    pcm_io.write_point_csv(
        Path(args.out_dir) / "pcm_points.csv", result,
        sample_rate=info.sample_rate, signal=x,
    )
    pcm_io.write_json_summary(
        Path(args.out_dir) / "pcm_summary.json", result=result, config=asdict(config),
        pcm=info, extra={"block_size": args.block_size},
    )
    print(f"  file              : {info.path}")
    print(f"  format            : {info.sample_format} {info.endian}-endian")
    print(f"  samples           : {info.n_samples}")
    print(f"  anomaly decisions : {result.n_anomalies}")
    print(f"  anomaly indices   : {result.anomaly_indices[:20].tolist()}"
          f"{' ...' if result.anomaly_indices.size > 20 else ''}")
    payload = result.as_dict()
    # Keep stdout payload compact: indices + counts only (full data in files).
    return {
        "input": {
            "path": info.path, "sample_format": info.sample_format,
            "endian": info.endian, "sample_rate": info.sample_rate,
            "n_samples": info.n_samples,
        },
        "n_anomalies": result.n_anomalies,
        "anomaly_indices": result.anomaly_indices.tolist(),
        "missing_rate": result.missing_rate,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    Path(args.out_dir).mkdir(parents=True, exist_ok=True)
    print("=== Sudden anomaly detection (offline) ===")
    print(f"  config: threshold={args.threshold} window={args.window_size} "
          f"prime={args.prime_size} min_history={args.min_history} "
          f"recent={args.recent_size} scale={args.scale} center={args.center}"
          + (f" block_size={args.block_size}" if args.block_size else " one-shot"))
    if args.pcm:
        payload = run_pcm(args)
    else:
        payload = {"scenarios": run_synthetic(args)}
    print(f"  outputs written to: {Path(args.out_dir).resolve()}")
    if args.json:
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
