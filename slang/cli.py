"""Command-line interface for the Slang toolchain.

Usage:
    python -m slang.cli lex       FILE
    python -m slang.cli parse     FILE
    python -m slang.cli analyze   FILE
    python -m slang.cli lower     FILE
    python -m slang.cli run       FILE [--ir | --source]   (default: --ir)
    python -m slang.cli compare   FILE
    python -m slang.cli serve     [--host 127.0.0.1] [--port 8080]
"""

from __future__ import annotations

import argparse
import json
import sys

from .analyzer import analyze
from .errors import LangError
from .ir_interp import IRInterpreter
from .lexer import tokenize
from .lower import lower_module
from .parser import parse
from .service import serve
from .source_interp import SourceInterpreter


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="slang", description="Slang scope/closure-conversion toolchain")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_lex = sub.add_parser("lex", help="print tokens as JSON")
    p_lex.add_argument("file")

    p_parse = sub.add_parser("parse", help="print AST as JSON")
    p_parse.add_argument("file")

    p_an = sub.add_parser("analyze", help="print scope/escape analysis as JSON")
    p_an.add_argument("file")

    p_low = sub.add_parser("lower", help="print closure-converted IR as JSON")
    p_low.add_argument("file")

    p_run = sub.add_parser("run", help="run a program")
    p_run.add_argument("file")
    mode = p_run.add_mutually_exclusive_group()
    mode.add_argument("--ir", action="store_const", const="ir", dest="mode")
    mode.add_argument("--source", action="store_const", const="source", dest="mode")
    p_run.set_defaults(mode="ir")

    p_cmp = sub.add_parser("compare", help="run both interpreters and diff traces")
    p_cmp.add_argument("file")

    p_serve = sub.add_parser("serve", help="start the JSON HTTP service")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)

    args = ap.parse_args(argv)

    if args.cmd == "serve":
        serve(args.host, args.port)
        return 0

    try:
        source = _read(args.file)
        if args.cmd == "lex":
            toks = tokenize(source, args.file)
            payload = [
                {"kind": t.kind, "value": t.value, "text": t.text, "loc": t.loc.to_dict()}
                for t in toks if t.kind != "EOF"
            ]
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        tree = parse(source, args.file)
        if args.cmd == "parse":
            print(json.dumps(tree.to_dict(), ensure_ascii=False, indent=2))
            return 0

        result = analyze(tree)
        if args.cmd == "analyze":
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0

        module = lower_module(tree, result)
        if args.cmd == "lower":
            print(json.dumps(module.to_dict(), ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "run":
            if args.mode == "source":
                trace = SourceInterpreter(tree).run()
            else:
                trace = IRInterpreter(module).run()
            for line in trace:
                print(line)
            return 0

        if args.cmd == "compare":
            ir_trace = IRInterpreter(module).run()
            src_trace = SourceInterpreter(tree).run()
            ok = ir_trace == src_trace
            print("source:", json.dumps(src_trace, ensure_ascii=False))
            print("ir    :", json.dumps(ir_trace, ensure_ascii=False))
            print("agree :", ok)
            return 0 if ok else 1

    except LangError as e:
        print(e.render(), file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
