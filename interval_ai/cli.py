"""Command-line interface.

Usage:
    python -m interval_ai.cli analyze program.ivl        # JSON to stdout
    python -m interval_ai.cli run program.ivl 3 -2 10    # concrete execution
    echo '...' | python -m interval_ai.cli analyze -
"""

from __future__ import annotations

import argparse
import json
import sys

from .concrete import run_source
from .errors import IvlError
from .pipeline import analyze_source, error_to_dict


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="interval_ai",
                                 description="Interval abstract interpreter")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="run interval analysis, print JSON")
    a.add_argument("path", help="source file ('-' for stdin)")
    a.add_argument("--indent", type=int, default=2)

    r = sub.add_parser("run", help="execute concretely with given inputs")
    r.add_argument("path")
    r.add_argument("inputs", nargs="*", type=int)

    args = ap.parse_args(argv)

    if args.path == "-":
        source = sys.stdin.read()
        display = "<stdin>"
    else:
        with open(args.path, "r", encoding="utf-8") as f:
            source = f.read()
        display = args.path

    if args.cmd == "analyze":
        try:
            res = analyze_source(source)
        except IvlError as e:
            print(json.dumps(error_to_dict(e), indent=args.indent))
            return 2
        payload = res.to_dict()
        payload["source_file"] = display
        print(json.dumps(payload, indent=args.indent))
        return 0

    try:
        cr = run_source(source, args.inputs)
    except IvlError as e:
        print(json.dumps(error_to_dict(e), indent=2))
        return 2
    out = {
        "consumed_inputs": cr.consumed_inputs,
        "variables": cr.env,
        "arrays": cr.arrays,
    }
    if cr.error is not None:
        out["runtime_error"] = {
            "kind": cr.error.kind,
            "message": str(cr.error),
            "location": (cr.error.span.to_dict()
                         if cr.error.span is not None else None),
        }
        print(json.dumps(out, indent=2))
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
