"""Command-line entry point: chunk-wise DC-bias removal on PCM files.

Examples
--------
Generate a synthetic biased sine, filter it in 1024-sample blocks, and
write the cleaned PCM plus a metrics JSON:

    python -m dc_blocker.cli \
        --synthetic biased-sine --sample-rate 48000 --seconds 2 \
        --freq 440 --amplitude 0.5 --bias 0.3 \
        --cutoff 5 --block-size 1024 \
        --output cleaned.s16 --metrics metrics.json

Filter an existing raw PCM file:

    python -m dc_blocker.cli --input raw.s16 --encoding s16le \
        --sample-rate 48000 --cutoff 5 --block-size 1024 \
        --output cleaned.s16 --metrics metrics.json
"""

from __future__ import annotations

import argparse
import json
import sys

import numpy as np

from .filter import DCBlocker
from .io import ENCODINGS, biased_sine, bias_step, read_pcm, write_pcm


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="dc-blocker", description=__doc__.splitlines()[0])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", help="input raw PCM file")
    src.add_argument(
        "--synthetic",
        choices=["biased-sine", "bias-step"],
        help="generate a synthetic input instead of reading a file",
    )
    p.add_argument("--encoding", choices=ENCODINGS, default="s16le")
    p.add_argument("--sample-rate", type=float, required=True)
    p.add_argument("--cutoff", type=float, default=5.0, help="high-pass cutoff in Hz")
    p.add_argument("--block-size", type=int, default=1024, help="samples per streaming block")
    # synthetic options
    p.add_argument("--seconds", type=float, default=2.0)
    p.add_argument("--freq", type=float, default=440.0)
    p.add_argument("--amplitude", type=float, default=0.5)
    p.add_argument("--bias", type=float, default=0.3)
    p.add_argument("--bias-after", type=float, default=-0.4, help="for bias-step")
    # outputs
    p.add_argument("--output", required=True, help="output raw PCM file")
    p.add_argument("--metrics", help="optional metrics JSON output path")
    p.add_argument("--dump-input", help="optionally also write the (synthetic) input PCM")
    return p.parse_args(argv)


def _blocks(x: np.ndarray, size: int):
    for start in range(0, len(x), size):
        yield x[start : start + size]


def run(argv: list[str] | None = None) -> dict:
    args = _parse_args(argv)
    if args.block_size < 1:
        raise ValueError("--block-size must be >= 1")

    if args.synthetic == "biased-sine":
        n = int(round(args.seconds * args.sample_rate))
        x = biased_sine(n, args.sample_rate, args.freq, args.amplitude, args.bias)
    elif args.synthetic == "bias-step":
        n = int(round(args.seconds * args.sample_rate))
        x = bias_step(n, n // 2, args.bias, args.bias_after)
    else:
        x = read_pcm(args.input, args.encoding)

    flt = DCBlocker(sample_rate=args.sample_rate, cutoff_hz=args.cutoff)
    y = np.concatenate(list(flt.process_stream(_blocks(x, args.block_size)))) if len(x) else x

    write_pcm(args.output, y, args.encoding)
    if args.dump_input:
        write_pcm(args.dump_input, x, args.encoding)

    tail = max(1, int(round(2 * flt.time_constant_samples)))
    steady = y[-tail:] if len(y) >= tail else y
    metrics = {
        "input_samples": int(len(x)),
        "block_size": args.block_size,
        "sample_rate": args.sample_rate,
        "cutoff_hz": args.cutoff,
        "pole_R": flt.coefficient,
        "time_constant_samples": flt.time_constant_samples,
        "settling_samples_1pct": flt.settling_samples(0.01),
        "input_mean": float(np.mean(x)) if len(x) else 0.0,
        "output_tail_mean": float(np.mean(steady)) if len(steady) else 0.0,
        "output_tail_abs_mean": float(np.mean(np.abs(steady))) if len(steady) else 0.0,
        "output_file": args.output,
    }
    if args.metrics:
        with open(args.metrics, "w", encoding="utf-8") as fh:
            json.dump(metrics, fh, indent=2)
    return metrics


def main(argv: list[str] | None = None) -> int:
    metrics = run(argv)
    json.dump(metrics, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
