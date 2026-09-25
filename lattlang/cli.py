"""Command-line interface for the LattLang toolchain.

Usage:
    python -m lattlang.cli parse     PROGRAM.lat
    python -m lattlang.cli ir        PROGRAM.lat       # non-SSA CFG IR
    python -m lattlang.cli ssa       PROGRAM.lat       # SSA IR
    python -m lattlang.cli analyze   PROGRAM.lat       # SCCP lattice dump
    python -m lattlang.cli optimize  PROGRAM.lat       # optimized IR + changes
    python -m lattlang.cli run       PROGRAM.lat [ast|ir|optimized]
    python -m lattlang.cli check     PROGRAM.lat       # equivalence report
    python -m lattlang.cli serve     [request.json]    # JSON on stdin/file
"""

from __future__ import annotations

import json
import sys

from .errors import LangError
from .interp_ast import run_ast
from .interp_ir import run_ir
from .ir import print_ir
from .irbuild import build_ir
from .optimize import optimize
from .parser import parse
from .analysis import run_sccp
from . import ssa as ssa_mod
from . import service


USAGE = __doc__


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd = argv[0]
    try:
        if cmd == "serve":
            return service.main(argv[1:])
        if len(argv) < 2:
            print(f"error: '{cmd}' requires a program file", file=sys.stderr)
            return 2
        source = _read(argv[1])
        return _dispatch(cmd, source, argv[2:])
    except LangError as e:
        loc = ""
        if e.span is not None and not e.span.synthetic:
            loc = f" at line {e.span.start_line}, col {e.span.start_col}"
        print(f"{e.stage} error{loc}: {e.message}", file=sys.stderr)
        return 1


def _dispatch(cmd: str, source: str, rest: list[str]) -> int:
    program = parse(source)

    if cmd == "parse":
        json.dump({"statements": len(program.body),
                   "span": program.span.to_json()}, sys.stdout, indent=2)
        print()
        return 0

    cfg = build_ir(program)
    if cmd == "ir":
        sys.stdout.write(print_ir(cfg))
        return 0

    ssa_prog = ssa_mod.build_ssa(cfg)
    if cmd == "ssa":
        sys.stdout.write(print_ir(ssa_prog))
        return 0

    res = run_sccp(ssa_prog)
    if cmd == "analyze":
        _print_analysis(ssa_prog, res)
        return 0

    if cmd == "optimize":
        report = optimize(ssa_prog, res)
        sys.stdout.write(print_ir(report.program))
        print(f"# {report.change_count()} change(s):", file=sys.stderr)
        for c in report.changes:
            print(f"#  - {json.dumps(c, ensure_ascii=False)}", file=sys.stderr)
        return 0

    if cmd == "run":
        mode = rest[0] if rest else "optimized"
        if mode == "ast":
            r = run_ast(program)
        elif mode in ("ir", "optimized"):
            target = ssa_prog if mode == "ir" else optimize(ssa_prog, res).program
            r = run_ir(target)
        else:
            print(f"error: unknown run mode {mode!r}", file=sys.stderr)
            return 2
        sys.stdout.write(r.stdout())
        if r.error is not None:
            print(f"runtime error: {r.error.message} (steps={r.steps})",
                  file=sys.stderr)
            return 1
        print(f"# completed in {r.steps} step(s)", file=sys.stderr)
        return 0

    if cmd == "check":
        report = service.action_check(source)
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0 if report["equivalent"] else 1

    print(f"error: unknown command {cmd!r}\n{USAGE}", file=sys.stderr)
    return 2


def _print_analysis(ssa_prog, res) -> None:
    consts = {k: v.value for k, v in res.lat.items() if v.is_const()}
    bottoms = sorted(k for k, v in res.lat.items() if v.is_bottom())
    print("reachable blocks:", sorted(res.reachable))
    print("executable edges:")
    for a, b in sorted(res.executable_edges):
        print(f"  {a} -> {b}")
    print("constants:")
    for k in sorted(consts):
        print(f"  {k} = {consts[k]}")
    print("non-constant:", " ".join(bottoms) if bottoms else "(none)")


if __name__ == "__main__":
    raise SystemExit(main())
