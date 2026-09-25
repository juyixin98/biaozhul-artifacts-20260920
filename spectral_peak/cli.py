"""Command-line interface: analyse a synthetic tone or a raw PCM file.

Examples:
  python -m spectral_peak.cli synth --freq 1000.5 --amp 0.8 --sr 48000 --n 4096
  python -m spectral_peak.cli pcm --file data.pcm --fmt s16le --sr 48000
"""

from __future__ import annotations

import argparse
import json
import sys

import numpy as np

from .analyzer import ESTIMATORS, analyze
from .pcm import DTYPES, read_pcm
from .signalgen import tone
from .windows import WINDOWS


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--sr", type=float, required=True, help="sample rate in Hz")
    p.add_argument("--window", choices=WINDOWS, default="hann")
    p.add_argument("--estimator", choices=ESTIMATORS, default="auto")
    p.add_argument("--min-peak-ratio", type=float, default=0.01)
    p.add_argument("--max-peaks", type=int, default=16)
    p.add_argument("--out", help="write JSON result to this file instead of stdout")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spectral_peak", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_synth = sub.add_parser("synth", help="generate a synthetic tone and analyse it")
    p_synth.add_argument("--freq", type=float, required=True, help="tone frequency in Hz")
    p_synth.add_argument("--amp", type=float, default=1.0)
    p_synth.add_argument("--phase", type=float, default=0.0)
    p_synth.add_argument("--dc", type=float, default=0.0, help="DC offset")
    p_synth.add_argument("--noise", type=float, default=0.0, help="Gaussian noise stddev")
    p_synth.add_argument("--n", type=int, default=4096, help="number of samples")
    _add_common(p_synth)

    p_pcm = sub.add_parser("pcm", help="analyse a raw (headerless) PCM file")
    p_pcm.add_argument("--file", required=True)
    p_pcm.add_argument("--fmt", choices=sorted(DTYPES), default="s16le")
    p_pcm.add_argument("--channels", type=int, default=1)
    p_pcm.add_argument("--channel", type=int, default=0)
    _add_common(p_pcm)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "synth":
        samples = tone(
            args.freq,
            amplitude=args.amp,
            sample_rate=args.sr,
            n=args.n,
            phase=args.phase,
            dc_offset=args.dc,
            noise_std=args.noise,
            seed=0,
        )
    else:
        samples = read_pcm(args.file, fmt=args.fmt, channels=args.channels,
                           channel=args.channel)

    result = analyze(
        np.asarray(samples),
        args.sr,
        window=args.window,
        estimator=args.estimator,
        min_peak_ratio=args.min_peak_ratio,
        max_peaks=args.max_peaks,
    )
    text = json.dumps(result, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
