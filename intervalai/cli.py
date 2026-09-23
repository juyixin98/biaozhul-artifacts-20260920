"""Command line interface for analyzing / executing Imp files.

Usage:
  python -m intervalai.cli analyze FILE --bound n=0..5 [--json] [--cfg]
  python -m intervalai.cli execute FILE -D n=3 [--json]
  python -m intervalai.cli parse   FILE [--cfg]

This is a convenience wrapper around the same pipeline the JSON service uses.
"""

import argparse
import json
import sys

from .errors import IvalError
from .pipeline import analyze_source, dump_cfg, execute_source, parse_program


def _parse_bound(text):
    # name=lo..hi (inclusive)
    try:
        name, rng = text.split("=", 1)
        lo, hi = rng.split("..", 1)
        lo, hi = int(lo), int(hi)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"bad bound {text!r}; expected NAME=LO..HI")
    if lo > hi:
        raise argparse.ArgumentTypeError(f"bound {text!r} has lo > hi")
    return name, (lo, hi)


def _parse_def(text):
    name, val = text.split("=", 1)
    return name, int(val)


def read_file(path):
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def cmd_analyze(a):
    src = read_file(a.file)
    bounds = dict(a.bound or [])
    rep = analyze_source(src, input_bounds=bounds or None,
                         include_points=not a.no_points,
                         include_cfg=a.cfg)
    if a.json:
        print(json.dumps(rep, indent=2))
        return 0
    return _print_analysis(rep)


def _print_analysis(rep):
    if rep.get("status") == "error":
        print(f"error [{rep['error']}] {rep['message']}", file=sys.stderr)
        return 2
    print(f"status              : {rep['status']}")
    print(f"loop headers        : {rep['loop_headers']}")
    print(f"ascending iterations: {rep['ascending_iterations']}")
    print(f"narrow rounds       : {rep['narrow_rounds']}")
    print(f"alarms              : {len(rep['alarms'])}")
    for al in rep["alarms"]:
        loc = al["loc"]
        print(f"  - [{al['severity']}] {al['kind']}/{al['subkind']} "
              f"at line {loc['line']}: {al['message']}")
    es = rep.get("exit_state")
    if es:
        scal = ", ".join(
            f"{k}={_fmt(v)}" for k, v in sorted(es["scalars"].items()))
        print(f"exit scalars        : {scal}")
        for name, a in sorted(es["arrays"].items()):
            print(f"exit array {name}[{a['size']}] elements: {_fmt(a['elements'])}")
    if not rep["alarms"]:
        print("no possible division-by-zero or array OOB detected")
    return 0


def _fmt(v):
    if v.get("bottom"):
        return "_|_"
    lo = "-oo" if v["lo"] is None else v["lo"]
    hi = "+oo" if v["hi"] is None else v["hi"]
    return f"[{lo}, {hi}]"


def cmd_execute(a):
    src = read_file(a.file)
    inputs = dict(a.defn or [])
    try:
        out = execute_source(src, inputs, step_limit=a.step_limit)
    except IvalError as e:
        print(f"error [{e.code}] {e.message}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"steps: {out['steps']}")
        for k, v in sorted(out["scalars"].items()):
            print(f"scalar {k} = {v}")
        for k, v in sorted(out["arrays"].items()):
            print(f"array  {k} = {v}")
    return 0


def cmd_parse(a):
    src = read_file(a.file)
    prog = parse_program(src)
    out = {"variables": [
               {"name": d.name, "init": d.init, "input": d.is_input}
               for d in prog.var_decls],
           "arrays": [{"name": x.name, "size": x.size} for x in prog.arr_decls],
           "statements": len(prog.body)}
    if a.cfg:
        from . import ir as ir_mod
        out["cfg"] = dump_cfg(ir_mod.lower(prog))
    print(json.dumps(out, indent=2))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="intervalai",
                                 description="Imp interval-analysis CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    an = sub.add_parser("analyze", help="interval analysis of a file")
    an.add_argument("file")
    an.add_argument("--bound", action="append", type=_parse_bound,
                    help="input bound NAME=LO..HI (repeatable)")
    an.add_argument("--json", action="store_true")
    an.add_argument("--cfg", action="store_true", help="include CFG dump")
    an.add_argument("--no-points", action="store_true")
    an.set_defaults(func=cmd_analyze)

    ex = sub.add_parser("execute", help="concrete reference execution")
    ex.add_argument("file")
    ex.add_argument("-D", "--defn", action="append", type=_parse_def,
                    help="input value NAME=INT (repeatable)")
    ex.add_argument("--step-limit", type=int, default=1_000_000)
    ex.add_argument("--json", action="store_true")
    ex.set_defaults(func=cmd_execute)

    pa = sub.add_parser("parse", help="parse and dump structure")
    pa.add_argument("file")
    pa.add_argument("--cfg", action="store_true")
    pa.set_defaults(func=cmd_parse)

    a = ap.parse_args(argv)
    try:
        return a.func(a)
    except IvalError as e:
        print(f"error [{e.code}] {e.message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
