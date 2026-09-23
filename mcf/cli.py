"""Command-line entry point: ``python -m mcf [request.json]``.

Reads the JSON request from the given file or standard input, writes the
JSON response to standard output.

Exit codes:
    0  status == "optimal"
    2  status == "invalid_request" (bad JSON, schema or range violation)
    3  negative_cycle or iteration_limit
    4  internal_error
"""

from __future__ import annotations

import json
import sys

from .api import solve_json_text


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) > 1:
        print(f"usage: python -m mcf [request.json]", file=sys.stderr)
        return 2
    if argv:
        try:
            with open(argv[0], "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            resp = {"status": "invalid_request", "error": f"cannot read file: {exc}"}
            print(json.dumps(resp, ensure_ascii=False, indent=2))
            return 2
    else:
        text = sys.stdin.read()

    out = solve_json_text(text)
    print(out)

    try:
        status = json.loads(out)["status"]
    except (json.JSONDecodeError, KeyError):
        return 4
    return {
        "optimal": 0,
        "invalid_request": 2,
        "negative_cycle": 3,
        "iteration_limit": 3,
    }.get(status, 4)


if __name__ == "__main__":
    sys.exit(main())
