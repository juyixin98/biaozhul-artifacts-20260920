"""Command-line interface for the MiniML toolchain.

Usage::

    python -m miniml.cli infer FILE [-q] [--no-value-restriction]
    python -m miniml.cli eval  FILE [--no-value-restriction]
    python -m miniml.cli lex   FILE
    python -m miniml.cli parse FILE

``-`` can be used as FILE to read source from standard input.
"""

from __future__ import annotations

import argparse
import sys

from .errors import render_compile_error
from .eval import eval_program
from .lexer import lex
from .pipeline import CompileFailure, compile_source
from .parser import parse
from .types import scheme_str, type_str


def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as f:
        return f.read()


def _fail(src: str, err: Exception) -> int:
    print(render_compile_error(src, err, filename=_SRC_NAME), file=sys.stderr)
    return 1


_SRC_NAME = "<input>"


def main(argv: list[str] | None = None) -> int:
    global _SRC_NAME
    ap = argparse.ArgumentParser(prog="miniml", description="MiniML toolchain")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("file")
        p.add_argument(
            "--no-value-restriction",
            action="store_true",
            help="disable the value restriction (unsound with references)",
        )
        p.add_argument("--no-trace", action="store_true", help="omit derivation steps")

    add_common(sub.add_parser("infer", help="parse and infer types"))
    add_common(sub.add_parser("eval", help="infer, then evaluate"))
    p_lex = sub.add_parser("lex", help="lex and print tokens")
    p_lex.add_argument("file")
    p_parse = sub.add_parser("parse", help="parse and print the AST")
    p_parse.add_argument("file")

    args = ap.parse_args(argv)
    src = _read(args.file)
    if args.file != "-":
        _SRC_NAME = args.file

    if args.cmd == "lex":
        try:
            for t in lex(src):
                print(t)
        except Exception as err:  # LexError
            return _fail(src, err)
        return 0

    if args.cmd == "parse":
        try:
            program = parse(src)
            for b in program.bindings:
                rec = "rec " if b.rec else ""
                print(f"let {rec}{b.name} = {b.value!r} ;;  @ {b.name_span.start}")
            if program.final_expr is not None:
                print(f"final: {program.final_expr!r}")
        except Exception as err:
            return _fail(src, err)
        return 0

    vr = not args.no_value_restriction
    trace = not args.no_trace
    try:
        program, result = compile_source(src, value_restriction=vr, trace=trace)
    except CompileFailure as exc:
        print(exc.rendered, file=sys.stderr)
        return 1

    for b in result.bindings:
        flag = "" if not b.monomorphic else "  (monomorphic: value restriction)"
        print(f"val {b.name} : {scheme_str(b.scheme)}{flag}")
    if result.expr_type is not None:
        print(f"- : {type_str(result.expr_type)}")

    if trace and result.trace:
        print("\n--- derivation ---")
        for i, ev in enumerate(result.trace, 1):
            loc = f"  [{ev.span.start}]" if ev.span is not None else ""
            print(f"{i:3d}. {ev.step:<12} {ev.detail}{loc}")

    if args.cmd == "eval":
        try:
            r = eval_program(program)
        except Exception as err:
            return _fail(src, err)
        print("\n--- evaluation ---")
        print(r.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
