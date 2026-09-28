"""Command-line client: isolate roots without standing up the HTTP server.

Examples (from the repository root)::

    .venv/bin/python -m scripts.isolate_cli --coeffs '[-6,11,-6,1]'
    .venv/bin/python -m scripts.isolate_cli --coeffs '[0,0,0]'
    .venv/bin/python -m scripts.isolate_cli --coeffs '[1,0,1]' --width 1/1000
    .venv/bin/python -m scripts.isolate_cli --file tests/fixtures/repeated_roots.json

Coefficients are ascending powers: index i multiplies x**i.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.service import isolate_coefficients
from app.settings import load_settings


def _response_to_dict(response: Any) -> dict[str, Any]:
    return {
        "request_id": response.request_id,
        "status": response.status,
        "degree": response.degree,
        "is_zero_polynomial": response.is_zero_polynomial,
        "roots": response.roots,
        "multiplicities": response.multiplicities,
        "evidence": response.evidence,
        "diagnostics": response.diagnostics,
        "failure": response.failure,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--coeffs", help="JSON array of rational coefficients")
    source.add_argument("--file", help="Path to a JSON request fixture")
    parser.add_argument("--width", default="1/1000000",
                        help="Target rational interval width")
    parser.add_argument("--lo", default=None, help="Optional search interval lo")
    parser.add_argument("--hi", default=None, help="Optional search interval hi")
    parser.add_argument("--no-evidence", action="store_true",
                        help="Skip the independent numeric evidence report")
    parser.add_argument("--request-id", default=None)
    args = parser.parse_args(argv)

    if args.file:
        payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
        coefficients = payload["coefficients"]
        width = payload.get("target_width", args.width)
        lo = payload.get("interval_lo", args.lo)
        hi = payload.get("interval_hi", args.hi)
        include_evidence = payload.get("include_evidence", not args.no_evidence)
        request_id = payload.get("request_id", args.request_id)
    else:
        coefficients = json.loads(args.coeffs)
        width, lo, hi = args.width, args.lo, args.hi
        include_evidence = not args.no_evidence
        request_id = args.request_id

    settings = load_settings()
    response = isolate_coefficients(
        coefficients=coefficients,
        settings=settings,
        target_width=width,
        interval_lo=lo,
        interval_hi=hi,
        request_id=request_id,
        include_evidence=include_evidence,
    )
    json.dump(_response_to_dict(response), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0 if response.status in ("ok", "zero_polynomial") else 1


if __name__ == "__main__":
    raise SystemExit(main())
