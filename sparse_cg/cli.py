"""Command-line interface: JSON request on stdin (or --input), JSON on stdout.

Usage
-----
    python -m sparse_cg.cli < request.json
    python -m sparse_cg.cli --input request.json --output response.json
    echo '<json>' | python -m sparse_cg.cli --pretty

Exit codes
----------
* 0  request was processed; *inspect result.converged* for numerical success
     (an unsuccessful solve -- e.g. non-positive curvature -- is still exit 0
     because the interface itself worked and diagnostics are returned).
* 2  request rejected (invalid JSON or failed validation, including illegal
     CSR / non-symmetric / non-positive-definite input).
* 3  unexpected internal error (a bug -- the message is included).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .api import handle_request
from .exceptions import SparseCgError


def _load_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        return {
            "__parse_error__": (
                f"request body is not valid JSON: {exc.msg} at line "
                f"{exc.lineno} column {exc.colno}"
            )
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sparse_cg",
        description="Preconditioned conjugate gradient for sparse SPD "
                    "systems (JSON in / JSON out).",
    )
    parser.add_argument(
        "-i", "--input",
        help="read the JSON request from this file instead of stdin",
    )
    parser.add_argument(
        "-o", "--output",
        help="write the JSON response to this file instead of stdout",
    )
    parser.add_argument(
        "-p", "--pretty", action="store_true",
        help="pretty-print the JSON response (indent 2)",
    )
    args = parser.parse_args(argv)

    if args.input:
        try:
            with open(args.input, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as exc:
            envelope = {
                "ok": False,
                "error": {
                    "code": "io_error",
                    "message": f"cannot read input file: {exc}",
                },
            }
            _emit(envelope, args)
            return 2
    else:
        raw = sys.stdin.read()

    payload = _load_json(raw)
    if isinstance(payload, dict) and "__parse_error__" in payload:
        envelope = {
            "ok": False,
            "error": {"code": "invalid_json",
                      "message": payload["__parse_error__"]},
        }
        _emit(envelope, args)
        return 2

    try:
        envelope = handle_request(payload)
    except SparseCgError as exc:
        envelope = {
            "ok": False,
            "error": {"code": getattr(exc, "code", "sparse_cg_error"),
                      "message": str(exc)},
        }
    except Exception as exc:  # pragma: no cover - defensive catch-all
        envelope = {
            "ok": False,
            "error": {"code": "internal_error",
                      "message": f"unexpected error: {type(exc).__name__}: {exc}"},
        }
        _emit(envelope, args)
        return 3

    _emit(envelope, args)
    return 0 if envelope["ok"] else 2


def _emit(envelope: dict[str, Any], args: argparse.Namespace) -> None:
    text = json.dumps(
        envelope, indent=2 if args.pretty else None,
        allow_nan=False, sort_keys=False,
    )
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
