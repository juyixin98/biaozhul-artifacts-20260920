"""Command-line JSON entry point.

Usage:
    python -m ccd < request.json
    python -m ccd request.json
    python -m ccd request.json -o response.json

Exit codes: 0 = success, 2 = invalid input.
"""

from __future__ import annotations

import argparse
import json
import sys

from ccd.api import solve_request


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ccd",
        description="Continuous collision detection for 2D circle trajectories (JSON in, JSON out).",
    )
    parser.add_argument("input", nargs="?", help="Request JSON file (default: stdin)")
    parser.add_argument("-o", "--output", help="Write response JSON to this file (default: stdout)")
    args = parser.parse_args(argv)

    try:
        if args.input is None:
            raw = sys.stdin.read()
        else:
            with open(args.input, "r", encoding="utf-8") as fh:
                raw = fh.read()
        request = json.loads(raw)
        response = solve_request(request)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2

    payload = json.dumps(response, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
