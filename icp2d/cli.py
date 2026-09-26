"""Command-line JSON entry point for the 2D ICP matcher.

Usage:
    python -m icp2d request.json            # read request from file
    python -m icp2d - < request.json        # read request from stdin
    cat request.json | python -m icp2d -    # same, via pipe

The JSON response is written to stdout. Exit codes:
    0  request valid, solver produced an estimate (check "status"/"uncertain")
    1  solver could not produce an estimate (e.g. insufficient inliers)
    2  invalid request (malformed JSON, missing fields, bad config)
"""

from __future__ import annotations

import argparse
import json
import sys

from .json_entry import run_request


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="icp2d",
        description="Point-to-point 2D ICP scan matching (JSON in, JSON out).",
    )
    parser.add_argument(
        "input",
        help="path to a JSON request file, or '-' to read from stdin",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="emit compact single-line JSON instead of indented output",
    )
    args = parser.parse_args(argv)

    try:
        if args.input == "-":
            payload = sys.stdin.read()
        else:
            with open(args.input, "r", encoding="utf-8") as handle:
                payload = handle.read()
    except OSError as exc:
        print(json.dumps({"success": False, "error": f"cannot read input: {exc}"}))
        return 2

    try:
        response = run_request(payload)
    except ValueError as exc:
        print(json.dumps({"success": False, "error": str(exc)}))
        return 2

    indent = None if args.compact else 2
    print(json.dumps(response, indent=indent))
    return 0 if response["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
