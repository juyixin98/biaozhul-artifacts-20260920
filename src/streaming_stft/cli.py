"""Command-line interface for the offline STFT service.

Usage::

    python -m streaming_stft.cli run --request examples/request_synth.json
    python -m streaming_stft.cli diagnose --nfft 256 --hop 256 --window hann

``run`` writes numeric artefacts into the request's ``output.dir`` and a
``metrics.json`` alongside them, and prints the metrics to stdout.
``diagnose`` evaluates the interior overlap-weight (COLA) coverage for a
window/hop pair without requiring an input signal.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from .service import parse_request, result_to_json, run_request
from .stft import STFTConfig, validate_config, window_diagnostic


def _cmd_run(args: argparse.Namespace) -> int:
    request_path = Path(args.request)
    with request_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    request = parse_request(data)
    result = run_request(request)

    metrics_path = Path(request.output_dir) / "metrics.json"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(result_to_json(result) + "\n", encoding="utf-8")

    payload = result.to_json_dict()
    payload["metrics_file"] = str(metrics_path)
    payload["source"] = request.source_description
    print(json.dumps(payload, indent=2, sort_keys=True))

    # Non-zero exit when the configuration cannot reconstruct the signal,
    # so automated callers can detect the condition.
    return 0 if result.covered else 2


def _cmd_diagnose(args: argparse.Namespace) -> int:
    config = STFTConfig(
        nfft=args.nfft,
        hop=args.hop,
        window=args.window,
        center=not args.no_center,
        pad_mode=args.pad_mode,
    )
    validate_config(config)

    # Evaluate on a long synthetic grid so the interior weight pattern is
    # stationary and unaffected by signal-boundary effects.
    probe_length = args.nfft * 20
    pad_left = config.nfft // 2 if config.center else 0
    dummy = np.zeros(probe_length)
    base_length = (
        2 * pad_left + probe_length if config.center else probe_length
    )
    n_frames = max(1, (base_length - config.nfft) // config.hop + 1)
    grid_length = (n_frames - 1) * config.hop + config.nfft
    diag = window_diagnostic(
        config, grid_length, n_frames, probe_length, pad_left
    )

    report = {
        "nfft": config.nfft,
        "hop": config.hop,
        "window": args.window,
        "center": config.center,
        "frames_evaluated": int(n_frames),
        "interior_all_covered": bool(diag.covered),
        "interior_weight_constant": bool(diag.weight_is_constant),
        "interior_weight_value": (
            diag.weight_constant_value
            if diag.weight_is_constant
            else None
        ),
        "interior_min_nonzero_weight": diag.min_nonzero_weight,
        "zero_weight_interior_positions_head": [
            int(i) for i in diag.zero_positions[:16]
        ],
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if diag.covered else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="streaming_stft",
        description="Offline streaming STFT / iSTFT service (NumPy, no UI).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="process one JSON request file")
    run_p.add_argument("--request", required=True, help="path to request JSON")
    run_p.set_defaults(func=_cmd_run)

    diag_p = sub.add_parser(
        "diagnose", help="evaluate overlap-weight (COLA) coverage"
    )
    diag_p.add_argument("--nfft", type=int, default=256)
    diag_p.add_argument("--hop", type=int, default=128)
    diag_p.add_argument("--window", default="hann")
    diag_p.add_argument("--pad-mode", default="reflect")
    diag_p.add_argument(
        "--no-center", action="store_true", help="disable centering padding"
    )
    diag_p.set_defaults(func=_cmd_diagnose)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
