"""Command-line entry point for the tensor memory planner service.

Usage::

    python -m tmem.cli examples/multi_consumer.json
    cat request.json | python -m tmem.cli -
    python -m tmem.cli examples/shared_view.json --enforce-budget \
        --save-report out/report.json

Exit codes: 0 success, 1 runtime error, 2 invalid request / budget exceeded.
"""

from __future__ import annotations

import argparse
import json
import sys

from .dag import DAGValidationError
from .planner import AllocationError, BudgetExceeded
from .service import handle_request


def _load_request(path: str) -> dict:
    if path == "-":
        return json.load(sys.stdin)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tmem",
        description="Static tensor DAG memory-reuse planner (add/matmul/slice).",
    )
    parser.add_argument(
        "request",
        help="path to a request JSON file, or '-' to read the request from stdin",
    )
    parser.add_argument(
        "--enforce-budget",
        action="store_true",
        help="exit non-zero if the planned arena exceeds peak_budget_elements",
    )
    parser.add_argument(
        "--save-report",
        metavar="PATH",
        default=None,
        help="write the full JSON report to PATH in addition to stdout",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="print only the compact summary, not the full report",
    )
    args = parser.parse_args(argv)

    try:
        request = _load_request(args.request)
        if not isinstance(request, dict):
            raise DAGValidationError("request must be a JSON object")
        if args.enforce_budget:
            request = {**request, "enforce_budget": True}
        report = handle_request(request)
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON: {exc}", file=sys.stderr)
        return 2
    except (DAGValidationError, AllocationError) as exc:
        kind = "budget" if isinstance(exc, BudgetExceeded) else "invalid request"
        print(f"error: {kind}: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:  # missing/dir path, permission, read failure
        print(f"error: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 2

    text = json.dumps(report, indent=2, sort_keys=False)
    if args.save_report:
        try:
            with open(args.save_report, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError as exc:
            print(
                f"error: cannot write report to {args.save_report!r}: "
                f"{exc.__class__.__name__}: {exc}",
                file=sys.stderr,
            )
            return 2

    if args.quiet:
        ex = report["execution"]
        cmp_ok = report["comparison"]["match"]
        safe = report["safety"]["simultaneously_live_windows_disjoint"]
        print(
            f"tensors={report['dag']['num_tensors']} "
            f"no_reuse_peak={ex['no_reuse']['peak_elements']}el "
            f"reuse_peak={ex['reuse']['peak_elements']}el "
            f"saved={ex['reduction_pct']}% "
            f"safety={'OK' if safe else 'VIOLATION'} "
            f"numeric_match={'OK' if cmp_ok else 'MISMATCH'}"
        )
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
