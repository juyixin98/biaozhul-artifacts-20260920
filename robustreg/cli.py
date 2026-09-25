"""Command-line JSON interface (no web framework, no frontend).

Usage::

    python -m robustreg.cli < request.json > response.json
    python -m robustreg.cli -f request.json -o response.json

Exit codes: 0 success, 1 invalid request / rank deficient, 2 unreadable JSON.
The JSON serializer rejects non-finite floats on purpose — every result the
library emits is finite, so a NaN/Inf here signals a bug rather than data.
"""

from __future__ import annotations

import argparse
import json
import sys

from robustreg.api import handle_request


def _json_default(obj):  # pragma: no cover - defensive
    raise TypeError(
        f"object of type {type(obj).__name__} is not JSON serializable"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="robustreg",
        description="Robust linear regression (Huber IRLS) JSON backend.",
    )
    parser.add_argument(
        "-f", "--file",
        help="input JSON file (default: standard input)",
    )
    parser.add_argument(
        "-o", "--output",
        help="output JSON file (default: standard output)",
    )
    args = parser.parse_args(argv)

    raw = sys.stdin.read() if args.file is None else open(args.file).read()
    try:
        request = json.loads(raw)
    except json.JSONDecodeError as exc:
        payload = {
            "ok": False,
            "error": {"code": "invalid_json", "message": str(exc)},
        }
        out = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                         indent=2)
        (sys.stdout if args.output is None else open(args.output, "w")) \
            .write(out + "\n")
        return 2

    response = handle_request(request)
    out = json.dumps(response, ensure_ascii=False, allow_nan=False,
                     indent=2, default=_json_default)
    if args.output is None:
        sys.stdout.write(out + "\n")
    else:
        with open(args.output, "w") as fh:
            fh.write(out + "\n")
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
