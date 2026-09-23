"""Command-line interface.

Usage::

    python -m regex_automata match PATTERN TEXT [--mode search|fullmatch|prefix]
    python -m regex_automata compile PATTERN [--nfa]
    python -m regex_automata serve [--host 127.0.0.1] [--port 8080]
"""
from __future__ import annotations

import argparse
import json
import sys

from .compiler import compile_pattern
from .errors import RegexError
from .service import _ast_dict, serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="regex_automata")
    sub = parser.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("match", help="match a text against a pattern")
    m.add_argument("pattern")
    m.add_argument("text")
    m.add_argument("--mode", choices=("search", "fullmatch", "prefix"),
                   default="search")

    c = sub.add_parser("compile", help="compile pattern and print JSON")
    c.add_argument("pattern")
    c.add_argument("--nfa", action="store_true", help="include full NFA dump")

    s = sub.add_parser("serve", help="run the JSON HTTP service")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)

    args = parser.parse_args(argv)

    if args.cmd == "serve":
        serve(args.host, args.port)
        return 0

    try:
        compiled = compile_pattern(args.pattern)
    except RegexError as exc:
        print(exc.render(), file=sys.stderr)
        return 2

    if args.cmd == "compile":
        out = {"pattern": args.pattern, "ast": _ast_dict(compiled.tree),
               "nfa_states": len(compiled.nfa.edges)}
        if args.nfa:
            out["nfa"] = compiled.nfa.to_dict()
            out["assertions"] = compiled.to_dict()["assertions"]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    result = getattr(compiled, args.mode)(args.text)
    if result is None:
        print(json.dumps({"matched": False, "match": None}, ensure_ascii=False))
        return 1
    print(json.dumps({"matched": True, "match": result.as_dict()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
