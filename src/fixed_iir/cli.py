"""Command-line interface to the offline fixed-point IIR service.

Examples
--------
Process a JSON request::

    fixed-iir run examples/request_stable.json -o out/response.json

Design + quick check without writing a request file::

    fixed-iir check --order 4 --cutoff 0.2 --coef 2,8

Generate a synthetic PCM input::

    fixed-iir synth --kind chirp -n 4096 -o out/chirp.pcm
"""

from __future__ import annotations

import argparse
import json
import sys

from . import signals as sigmod
from .biquad import FilterConfig
from .design import butter_lowpass, cheby1_lowpass
from .qformat import QFormat
from .service import process_request_file
from .stability import assess_stability


def _parse_q(text: str) -> QFormat:
    parts = text.replace(",", " ").split()
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("Q format must be 'int_bits,frac_bits', e.g. 2,14")
    return QFormat(int(parts[0]), int(parts[1]))


def _cmd_run(args: argparse.Namespace) -> int:
    resp = process_request_file(args.request, args.output)
    if not resp.get("ok"):
        print(json.dumps(resp, indent=2))
        return 2
    if args.print_response:
        print(json.dumps(resp, indent=2))
    else:
        s = resp["stability"]
        print(f"verdict: {s['verdict']}  "
              f"max |z| float={s['max_pole_radius_float']:.6f} "
              f"quant={s['max_pole_radius_quantized']:.6f}")
        print(f"output files: {resp['output']['files']}")
        for w in s["warnings"]:
            print(f"WARNING: {w}")
    return 0 if resp["stability"]["verdict"] != "unsafe" else 1


def _cmd_check(args: argparse.Namespace) -> int:
    if args.type == "butter":
        sos = butter_lowpass(args.order, args.cutoff)
    else:
        sos = cheby1_lowpass(args.order, args.cutoff, args.ripple)
    cfg = FilterConfig(
        q_sig=args.sig, q_coef=args.coef, guard_bits=args.guard_bits,
        rounding=args.rounding,
    )
    rep = assess_stability(sos, cfg)
    print(f"filter: {args.type} order={args.order} cutoff={args.cutoff}")
    print(f"q_sig={cfg.q_sig} q_coef={cfg.q_coef} guard={cfg.guard_bits} "
          f"rounding={cfg.rounding}")
    for s in rep.poles_quantized.sections:
        print(f"  section {s.index}: radii={[f'{r:.6f}' for r in s.radii]} "
              f"jury_stable={s.jury_stable}")
    for c in rep.limit_cycles:
        if c.found:
            print(f"  section {c.section_index}: {c.kind} limit cycle "
                  f"amp={c.amplitude:.4g} period={c.period}")
    print(f"verdict: {rep.verdict}")
    for w in rep.warnings:
        print(f"WARNING: {w}")
    return 0 if rep.verdict != "unsafe" else 1


def _cmd_synth(args: argparse.Namespace) -> int:
    n = args.n
    if args.kind == "sine":
        x = sigmod.sine(n, args.freq, args.amplitude)
    elif args.kind == "chirp":
        x = sigmod.chirp(n, 0.0, 0.5, args.amplitude)
    elif args.kind == "impulse":
        x = sigmod.impulse(n, args.amplitude)
    elif args.kind == "noise":
        x = sigmod.noise(n, args.amplitude)
    else:
        raise ValueError(args.kind)
    sigmod.write_pcm(args.output, x, args.dtype)
    print(f"wrote {n} {args.kind} samples ({args.dtype}) -> {args.output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fixed-iir", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="process a JSON request file")
    pr.add_argument("request")
    pr.add_argument("-o", "--output", default=None, help="response JSON path")
    pr.add_argument("-v", "--print-response", action="store_true")
    pr.set_defaults(func=_cmd_run)

    pc = sub.add_parser("check", help="design a filter and assess quantization risk")
    pc.add_argument("--type", choices=["butter", "cheby1"], default="butter")
    pc.add_argument("--order", type=int, default=4)
    pc.add_argument("--cutoff", type=float, default=0.2)
    pc.add_argument("--ripple", type=float, default=1.0)
    pc.add_argument("--sig", type=_parse_q, default=QFormat(1, 15))
    pc.add_argument("--coef", type=_parse_q, default=QFormat(2, 14))
    pc.add_argument("--guard-bits", type=int, default=2)
    pc.add_argument("--rounding",
                    choices=["truncate", "floor", "half_up", "convergent"],
                    default="convergent")
    pc.add_argument("--overflow", choices=["saturate", "wrap"], default="saturate")
    pc.set_defaults(func=_cmd_check)

    ps = sub.add_parser("synth", help="write a synthetic PCM file")
    ps.add_argument("--kind", choices=["sine", "chirp", "impulse", "noise"],
                    default="sine")
    ps.add_argument("-n", type=int, default=4096)
    ps.add_argument("--freq", type=float, default=0.05)
    ps.add_argument("--amplitude", type=float, default=0.9)
    ps.add_argument("--dtype", default="int16", choices=["int16", "int32", "uint8"])
    ps.add_argument("-o", "--output", required=True)
    ps.set_defaults(func=_cmd_synth)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
