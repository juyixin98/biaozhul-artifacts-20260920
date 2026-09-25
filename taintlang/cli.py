"""Command line interface.

Usage::

    python -m taintlang.cli analyze FILE [--k K] [--no-ir]
    python -m taintlang.cli serve   [--host H] [--port P]
"""

from __future__ import annotations

import argparse
import json
import sys

from .errors import TaintLangError
from .pipeline import analyze_source
from .service import serve


def _load(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def cmd_analyze(args) -> int:
    source = _load(args.file)
    config_data = {"k": args.k}
    if args.entry:
        config_data["entry_points"] = args.entry
    from .config import Config
    config = Config.from_dict(config_data)
    try:
        result = analyze_source(
            source, config=config,
            tainted_entry_params=not args.no_tainted_entry_params)
    except TaintLangError as exc:
        print(json.dumps({"ok": False, "error": exc.to_dict()},
                         ensure_ascii=False, indent=2))
        return 2
    if args.no_ir:
        result.pop("ir", None)
    if args.summary:
        s = result["stats"]
        print(f"findings: {s['findings']}  "
              f"contexts: {s['contexts']}  "
              f"block steps: {s['block_steps']}  "
              f"flow nodes: {s['flow_nodes']} edges: {s['flow_edges']}")
        for f in result["findings"]:
            print(f"  ALARM {f['sink']['marker']} @ "
                  f"{f['sink']['span']['start']['line']}:"
                  f"{f['sink']['span']['start']['column']}  <- "
                  f"{f['source']['kind']} @ "
                  f"{f['source']['span']['start']['line']} "
                  f"(paths={f['path_count']})")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_serve(args) -> int:
    serve(args.host, args.port, verbose=args.verbose)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="taintlang",
                                     description="Cross-function taint analyzer")
    sub = parser.add_subparsers(dest="command", required=True)

    a = sub.add_parser("analyze", help="analyze a source file and print JSON")
    a.add_argument("file", help="TaintLang source file (.tl)")
    a.add_argument("--k", type=int, default=2,
                   help="call-string context depth (default 2)")
    a.add_argument("--entry", action="append", default=[],
                   help="entry function name (repeatable; default: first func)")
    a.add_argument("--no-ir", action="store_true", help="omit IR dump")
    a.add_argument("--no-tainted-entry-params", action="store_true",
                   help="do not treat entry-function parameters as taint")
    a.add_argument("--summary", action="store_true",
                   help="print a short text summary instead of JSON")
    a.set_defaults(func=cmd_analyze)

    s = sub.add_parser("serve", help="run the JSON/HTTP service")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_serve)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
