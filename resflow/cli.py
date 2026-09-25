"""Command line interface: file analysis and the JSON service."""

import argparse
import json
import sys

from .errors import ResFlowError
from .engine import analyze
from .service import serve


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="resflow",
        description="ResFlow resource-release path analyzer")
    sub = parser.add_subparsers(dest="command", required=True)

    p_analyze = sub.add_parser("analyze", help="analyze a .rf source file")
    p_analyze.add_argument("source_file", help="path to the ResFlow source")
    p_analyze.add_argument("--loop-bound", type=int, default=2,
                           help="max loop re-entries per explored path")
    p_analyze.add_argument("--max-paths", type=int, default=512,
                           help="global path budget")
    p_analyze.add_argument("--indent", type=int, default=2,
                           help="JSON indentation (0 disables indentation)")

    p_serve = sub.add_parser("serve", help="run the JSON HTTP service")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)

    args = parser.parse_args(argv)

    if args.command == "serve":
        serve(args.host, args.port)
        return 0

    try:
        with open(args.source_file, "r", encoding="utf-8") as fh:
            source = fh.read()
    except OSError as exc:
        print(json.dumps({"error": {"code": "io_error",
                                    "message": str(exc)}}, indent=2),
              file=sys.stderr)
        return 2

    try:
        report = analyze(source, loop_bound=args.loop_bound,
                         max_paths=args.max_paths)
    except ResFlowError as exc:
        print(json.dumps({"error": {
            "code": "analysis_frontend_error",
            "kind": type(exc).__name__,
            **exc.to_dict()}}, indent=2), file=sys.stderr)
        return 1

    indent = args.indent if args.indent > 0 else None
    print(json.dumps(report, indent=indent))
    return 0


if __name__ == "__main__":
    sys.exit(main())
