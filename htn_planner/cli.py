"""Command-line entry point: plan a problem from fixture/rule files.

Prints a structured JSON document containing the planner verdict, expansion
tree, action sequence, failure/uncertain evidence and the independent
verification report.  Optionally persists to a SQLite evidence store.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from .core.engine import Bounds
from .lang import LangError, parse_domain, parse_problem
from .logging_setup import configure_logging, request_id_var
from .service import PlanningService, RequestError
from .store import EvidenceStore
from .verify import Verifier


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="htn-plan", description="Bounded HTN planner (local, offline)"
    )
    parser.add_argument("--domain", required=True, type=Path, help="domain rule file")
    parser.add_argument("--problem", required=True, type=Path, help="problem rule file")
    parser.add_argument("--domain-version", default="local-unversioned")
    parser.add_argument("--request-id", default=None)
    parser.add_argument("--db", default=None, help="optional SQLite evidence DB path")
    parser.add_argument("--max-depth", type=int, default=12)
    parser.add_argument("--max-expansions", type=int, default=500)
    parser.add_argument("--max-actions", type=int, default=100)
    parser.add_argument("--max-search-nodes", type=int, default=5000)
    parser.add_argument(
        "--compact", action="store_true", help="compact single-line JSON output"
    )
    return parser


def run(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    request_id = args.request_id or f"cli-{uuid.uuid4().hex[:12]}"
    configure_logging("WARNING")  # CLI emits JSON on stdout; keep logs quiet
    try:
        domain = parse_domain(args.domain.read_text())
        problem = parse_problem(args.problem.read_text())
    except (LangError, OSError) as exc:
        print(
            json.dumps(
                {"request_id": request_id, "error": "invalid_input", "detail": str(exc)}
            ),
            file=sys.stderr,
        )
        return 2

    bounds = Bounds(
        max_depth=args.max_depth,
        max_expansions=args.max_expansions,
        max_actions=args.max_actions,
        max_search_nodes=args.max_search_nodes,
    )
    store = EvidenceStore(args.db) if args.db else EvidenceStore(":memory:")
    service = PlanningService(store, bounds)
    token = request_id_var.set(request_id)
    try:
        response = service.plan(
            domain_text=args.domain.read_text(),
            problem_text=args.problem.read_text(),
            request_id=request_id,
            domain_version=args.domain_version,
        )
    except RequestError as exc:
        print(
            json.dumps(
                {"request_id": request_id, "error": "invalid_request", "detail": exc.message}
            ),
            file=sys.stderr,
        )
        return 2
    finally:
        request_id_var.reset(token)

    payload: dict[str, Any] = response.to_dict()
    payload["request_id"] = request_id
    indent = None if args.compact else 2
    print(json.dumps(payload, indent=indent, ensure_ascii=False))
    store.close()
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
